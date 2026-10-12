"""The rootless Podman deployment harness, ``make verify-podman-deploy`` (spec D12, D13).

It builds the five runtime images from the working tree, installs a run-unique test stack
through the real installer (``deploy/podman/aegis-podman``) under the user's systemd
manager, checks it, and removes exactly what it recorded. Nothing it creates can start at
boot: the installer renders a test prefix's units without ``[Install]`` sections, nothing is
ever enabled, and no container has a Podman restart policy.

Each check prints one line, ``AEGIS_PODMAN_DEPLOY check=<name> status=passed|failed
[detail]``, and no line carries a secret. The run record,
``${XDG_STATE_HOME:-~/.local/state}/aegis-podman-deploy-check/<run>.json`` (0600), names every
identity the run may create before it creates it, and the run's own directory (the
installer's state home, the synthetic root and the derivatives directory init prepares) is
``…/aegis-podman-deploy-check/<run>/``.
Both survive a reboot, so ``--cleanup <record>`` finishes an interrupted run's cleanup by
those exact identities; it changes nothing when one of them no longer matches. Addresses and
ports are discovered at run time and never written into the repository.

Images are built with plain ``podman build``, as CI's publish job builds them with ``docker
build``: the tracked Dockerfiles, the repository root as context and default build
arguments. The e2e harness builds through Compose only because it then runs the stack with
Compose; its launch guard gates runtime launches that mount originals and passes builds
through unchanged.

``--rehearse-level-migration`` rehearses the one-time move of an install's volumes to its
shared SELinux level instead of running the checks. It installs the release at
``--from-commit`` (default: the last installer before the shared level) with that commit's
own installer and images, writes a marker administrator, upgrades in place with this tree's
installer and images, and checks that PostgreSQL comes back healthy with the marker. The old
installer cannot install this tree's images (their renderer is newer than it understands), so
the commit's tree is extracted with ``git archive`` into the run's directory and its four
images are built from it, labelled for the run like the others.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import http.client
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, TextIO

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:  # run as a file: scripts/podman_deploy_check.py
    sys.path.insert(0, str(REPOSITORY))

from scripts.podman_dev import inventory  # noqa: E402

INSTALLER = REPOSITORY / "deploy" / "podman" / "aegis-podman"
TEMPLATES = REPOSITORY / "deploy" / "podman" / "quadlet"
DOCKERFILES = REPOSITORY / "docker"
INSTALLER_MODULE = "aegis_podman_installer"
PODMAN = ("podman", "--remote=false")
SYSTEMCTL = ("systemctl", "--user")
ROLES = ("backend", "gateway", "postgres", "caddy", "media")
PRODUCTION_PREFIX = "aegis-"
# One run: t + 8 hex. Its prefix, release, tags and directory all derive from it.
RUN_ID = re.compile(r"t[0-9a-f]{8}")
TEST_PREFIX = re.compile(r"aegis-t[0-9a-f]{8}-")
STATE_DIRECTORY = "aegis-podman-deploy-check"
LOCK_NAME = "lock"
# A random token the run writes into its directory: with the inode, that directory's
# identity, which (unlike the device number) a reboot does not change.
DIRECTORY_TOKEN = ".aegis-deploy-check-run"
# Every image the run builds carries this label with the run ID, so its image ID is the
# run's own and cleanup removes only an image that carries it.
RUN_LABEL = "io.aegis.deploy-check.run"
# The e2e and Compose harnesses' ports: a listener there means another test stack is up.
RESERVED_PORTS = (18080, 55432)
TEST_PORTS = range(20000, 32000)
PORT_ATTEMPTS = 64
MINIMUM_FREE_BYTES = 40 * 2**30
MINIMUM_SYSTEMD = 254
# A documentation address (RFC 5737): asking the kernel for its route names the host's
# primary IPv4 address as the route's preferred source, and nothing is sent.
ROUTE_PROBE = "192.0.2.1"
LOOPBACK = "127.0.0.1"
SLOT = "deploy-check"
SLOT_TARGET = f"/srv/aegis/roots/{SLOT}"
TEST_READER_TYPE = "container_t"
CONTAINER_FILE_TYPE = "container_file_t"
ROOT_ENTRIES = ("album", "notes.txt")
ROOT_FILE = "notes.txt"
WRITE_PROBE = ".aegis-deploy-check-write"
CSRF_PATH = "/api/v1/auth/csrf"
LOGIN_PATH = "/api/v1/auth/login"
# deploy/nginx sets no limit_req_status, so the gateway's login limit answers 503; 429 is
# accepted too, as the status a limit would send if that ever changed. A POST without a
# CSRF token is refused by Django (403) once it passes the limit, so no login is attempted.
LIMITED = frozenset({429, 503})
THROTTLE_REQUESTS = 30
# limit_req admits 10 requests a minute: 13 s frees two places in a drained bucket.
THROTTLE_COOLDOWN = 13.0
POLL_SECONDS = 5.0
START_TIMEOUT = 900
RECOVERY_DEADLINE = 600.0
STOP_DEADLINE = 300.0
MIGRATE_FAILURE_DEADLINE = 240.0
CA_DEADLINE = 60.0
HTTPS_DEADLINE = 60.0
BUILD_TIMEOUT = 3600
# Builds run at a low CPU and I/O priority: the host serves production meanwhile.
GENTLE = ("nice", "-n", "10", "ionice", "-c", "2", "-n", "7")
# Another run's unit files: reported at preflight, never touched.
OTHER_RUN = re.compile(r"aegis-t[0-9a-f]{8}-")
# systemctl is-enabled states that would start a unit at boot.
BOOT_STATES = frozenset({"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"})
REMOTE_VARIABLES = frozenset({
    "DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION",
})
CHECKS = (
    "units_healthy", "not_boot_enabled", "no_restart_policy", "gateway_unpublished",
    "tls_local_ca", "throttle_per_address", "root_read_only", "media_processor_isolated",
    "shared_volume_exchange", "postgres_restart_recovers", "migrate_failure_recovers",
)
# Lists the synthetic root and reads one of its files as the indexer's user, then tries to
# create a file in it; prints what it saw as JSON. The write must fail with EROFS.
ROOT_PROBE = (
    "import errno, json, os, sys\n"
    "root, name, probe = sys.argv[1:4]\n"
    "seen = {'entries': sorted(os.listdir(root))}\n"
    "with open(os.path.join(root, name), 'rb') as handle:\n"
    "    seen['read'] = handle.read(256).decode('ascii', 'replace')\n"
    "try:\n"
    "    os.close(os.open(os.path.join(root, probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL,"
    " 0o600))\n"
    "    seen['write'] = 'created'\n"
    "except OSError as error:\n"
    "    seen['write'] = errno.errorcode.get(error.errno, str(error.errno))\n"
    "print(json.dumps(seen))\n"
)
# Inside the media processor (spec M2): its network interfaces, every file under
# /run/secrets, an attempt to create a file in the root (which must fail with EROFS) and its
# no_new_privs flag; prints what it saw as JSON.
PROCESSOR_PROBE = (
    "import errno, json, os, sys\n"
    "root, probe = sys.argv[1:3]\n"
    "with open('/proc/net/dev', encoding='ascii') as handle:\n"
    "    rows = handle.read().splitlines()[2:]\n"
    "seen = {'interfaces': sorted(row.split(':', 1)[0].strip() for row in rows)}\n"
    "secrets = []\n"
    "def refused(error):\n"
    "    if error.errno != errno.ENOENT:  # no /run/secrets at all is no secret\n"
    "        secrets.append('unreadable: ' + errno.errorcode.get(error.errno, str(error.errno)))\n"
    "for directory, _, files in os.walk('/run/secrets', onerror=refused):\n"
    "    secrets += [os.path.join(directory, name) for name in files]\n"
    "seen['secrets'] = sorted(secrets)\n"
    "try:\n"
    "    os.close(os.open(os.path.join(root, probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL,"
    " 0o600))\n"
    "    seen['write'] = 'created'\n"
    "except OSError as error:\n"
    "    seen['write'] = errno.errorcode.get(error.errno, str(error.errno))\n"
    "with open('/proc/self/status', encoding='ascii') as handle:\n"
    "    seen['no_new_privs'] = next((line.split(':', 1)[1].strip() for line in handle\n"
    "                                 if line.startswith('NoNewPrivs:')), None)\n"
    "print(json.dumps(seen))\n"
)

# The shared-volume check's file, written by the media worker into scratch and into the
# derivatives, read back by the processor and the gateway, then removed. Each run's init draws
# its own MCS level, which every container of that test stack shares. A fresh install records
# no volume before its level, so the installer's one-time move of existing volumes to the
# shared level (for a record from before it) never runs in a checked stack: the rehearsal
# (--rehearse-level-migration) runs it.
EXCHANGE_NAME = ".aegis-deploy-check-exchange"
EXCHANGE_SCRATCH = f"/srv/aegis/scratch/{EXCHANGE_NAME}"
EXCHANGE_DERIVATIVE = f"/srv/aegis/derivatives/{EXCHANGE_NAME}"
WRITE_EXCHANGE = (
    "import os, sys\n"
    "path, text = sys.argv[1:3]\n"
    "descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)\n"
    "try:\n"
    "    os.write(descriptor, text.encode('ascii'))\n"
    "finally:\n"
    "    os.close(descriptor)\n"
)
REMOVE_EXCHANGE = (
    "import os, sys\n"
    "for path in sys.argv[1:]:\n"
    "    if os.path.lexists(path):\n"
    "        os.unlink(path)\n"
)

# The rehearsal (--rehearse-level-migration). Its default commit has the last installer
# before the shared SELinux level, whose renderer is API 1 and whose releases have four images.
PRE_LEVEL_COMMIT = "c60a2f4"
COMMIT = re.compile(r"[0-9a-f]{7,40}")
PREVIOUS_ROLES = ("backend", "gateway", "postgres", "caddy")
REHEARSAL_CHECKS = (
    "level_migration_previous_install", "level_migration_marker", "level_migration_upgrade",
    "level_migration_start", "level_migration_marker_survives",
)
# The marker: an administrator bootstrap-password creates before the upgrade and must find
# again after it. Its password is generated and kept by the installer, never seen here.
MARKER_USER = "deploy-check"
MARKER_EMAIL = "deploy-check@example.org"
# bootstrap_admin prints the user's UUID primary key.
BOOTSTRAPPED = re.compile(
    r"user_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) created=(true|false)"
)
LABEL_LEVEL = re.compile(r"s0:c\d{1,4},c\d{1,4}")
# The upgrade stops the stack and relabels every recorded volume once.
UPGRADE_TIMEOUT = 3600

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class HarnessError(RuntimeError):
    """The harness refuses, or a step it depends on failed."""


class CheckFailed(RuntimeError):
    """One check failed; its message is the check's detail."""


class Interrupted(KeyboardInterrupt):
    """SIGTERM or SIGHUP. Like Ctrl-C, it gives a running child a moment to exit."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class Response:
    status: int
    # The local address the connection left from.
    source: str


def run_command(
    arguments: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one program as an argument vector, never through a shell, with nothing on stdin."""
    return subprocess.run(
        list(arguments), stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=timeout, check=False, env=None if env is None else dict(env),
    )


def https_request(
    address: str, port: int, method: str, path: str, cafile: Path, *,
    body: bytes | None = None, timeout: float = 15.0,
) -> Response:
    """One HTTPS request to ``address``, verified against ``cafile`` alone.

    The certificate must name ``address`` (an IP literal, so no SNI is sent). The kernel
    picks the connection's source address: connecting to an address of this host leaves
    from that same address.
    """
    context = ssl.create_default_context(cafile=str(cafile))
    connection = http.client.HTTPSConnection(address, port, timeout=timeout, context=context)
    try:
        connection.connect()
        assert connection.sock is not None
        source = str(connection.sock.getsockname()[0])
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response.read(1 << 16)
        return Response(response.status, source)
    finally:
        connection.close()


def load_installer() -> ModuleType:
    """The installer as a module, for its unit layout; it never runs from here."""
    loaded = sys.modules.get(INSTALLER_MODULE)
    if loaded is not None:
        return loaded
    loader = SourceFileLoader(INSTALLER_MODULE, str(INSTALLER))
    spec = spec_from_loader(INSTALLER_MODULE, loader)
    if spec is None:
        raise HarnessError(f"cannot load {INSTALLER}")
    module = module_from_spec(spec)
    # Dataclasses resolve string annotations through sys.modules during class creation.
    sys.modules[INSTALLER_MODULE] = module
    try:
        loader.exec_module(module)
    except BaseException:
        del sys.modules[INSTALLER_MODULE]
        raise
    return module


def port_in_use(port: int) -> bool:
    """Whether anything listens on ``port`` on any address; unknown counts as in use."""
    try:
        return bool(load_installer().port_in_use(port))
    except Exception:
        return True


def free_bytes(path: str) -> int:
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def random_ports() -> list[int]:
    return random.SystemRandom().sample(TEST_PORTS, PORT_ATTEMPTS)


@dataclass
class Machine:
    """Everything outside the harness's own code, so tests replace it with fakes."""

    run: Runner = run_command
    request: Callable[..., Response] = https_request
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    uid: int = field(default_factory=os.getuid)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    port_in_use: Callable[[int], bool] = port_in_use
    free_bytes: Callable[[str], int] = free_bytes
    port_candidates: Callable[[], Iterable[int]] = random_ports
    token: Callable[[int], str] = secrets.token_hex
    selinux_enforce: Path = Path("/sys/fs/selinux/enforce")
    # The interpreter that runs the installer; it also runs the release's aegisctl.
    python: str = field(default_factory=lambda: sys.executable or "python3")
    # The run's log in the state directory, while a run or --cleanup holds the lock.
    log: TextIO | None = None


# -- the run's identities ----------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Every unit file, drop-in directory, unit and Podman object a prefix's stack has."""

    unit_files: tuple[str, ...]
    drop_in_directories: tuple[str, ...]
    services: tuple[str, ...]
    oneshots: tuple[str, ...]
    socket: str
    generated: tuple[str, ...]
    containers: dict[str, str]
    health_checked: tuple[str, ...]
    volumes: tuple[str, ...]
    networks: tuple[str, ...]

    @property
    def units(self) -> tuple[str, ...]:
        return (self.socket, *self.services, *self.oneshots, *self.generated)


def checked_prefix(prefix: str) -> str:
    """A run's prefix: never the production prefix, always aegis-t<8 hex>-."""
    if prefix == PRODUCTION_PREFIX or TEST_PREFIX.fullmatch(prefix) is None:
        raise HarnessError(
            f"refusing the prefix {prefix!r}: the harness only ever uses aegis-t<8 hex>-, "
            f"never the production {PRODUCTION_PREFIX}"
        )
    return prefix


def _template_keys(text: str) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, separator, value = stripped.partition("=")
        if separator:
            keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def _named(keys: Mapping[str, list[str]], key: str, prefix: str, template: str) -> str:
    values = keys.get(key, [])
    if len(values) != 1:
        raise HarnessError(f"the template {template} must set {key} exactly once")
    name = values[0].replace("@@PREFIX@@", prefix)
    if "@@" in name or not name.startswith(prefix):
        raise HarnessError(f"the template {template} names {key}={values[0]}")
    return name


def config_home(machine: Machine) -> Path:
    """Where systemd and Quadlet read the user's units, as the installer resolves it."""
    try:
        return Path(load_installer().Paths.resolve(machine.home, machine.environ).config)
    except Exception as error:
        raise HarnessError(str(error)) from None


def layout_for(prefix: str, config: Path) -> Layout:
    """Read the templates the installer renders: names, unit kinds and health checks."""
    installer = load_installer()
    unit_files: list[str] = []
    services: list[str] = []
    oneshots: list[str] = []
    generated: list[str] = []
    containers: dict[str, str] = {}
    health: list[str] = []
    volumes: list[str] = []
    networks: list[str] = []
    socket = None
    for template in sorted(installer.TEMPLATE_NAMES):
        keys = _template_keys((TEMPLATES / template).read_text(encoding="utf-8"))
        file_name = f"{prefix}{template}"
        unit_files.append(str(config / installer.unit_directory(template) / file_name))
        stem, _, suffix = file_name.rpartition(".")
        if suffix == "container":
            service = f"{stem}.service"
            containers[service] = _named(keys, "ContainerName", prefix, template)
            (oneshots if keys.get("Type") == ["oneshot"] else services).append(service)
            if keys.get("HealthCmd"):
                health.append(service)
        elif suffix == "volume":
            volumes.append(_named(keys, "VolumeName", prefix, template))
            generated.append(f"{stem}-volume.service")
        elif suffix == "network":
            networks.append(_named(keys, "NetworkName", prefix, template))
            generated.append(f"{stem}-network.service")
        elif suffix == "socket":
            socket = file_name
    if socket is None:
        raise HarnessError("the templates define no socket")
    quadlet = config / installer.UNIT_DIRECTORIES[".container"]
    drop_ins = tuple(str(quadlet / f"{prefix}{role}.container.d") for role in installer.ROOT_ROLES)
    return Layout(
        tuple(unit_files), drop_ins, tuple(services), tuple(oneshots), socket,
        tuple(generated), containers, tuple(health), tuple(volumes), tuple(networks),
    )


@dataclass
class Record:
    """What one run may create, recorded before it is created."""

    run: str
    prefix: str
    release: str
    run_directory: str
    port: int
    unit_files: list[str]
    drop_in_directories: list[str]
    units: list[str]
    containers: list[str]
    volumes: list[str]
    networks: list[str]
    # role -> {"tag": ..., "id": <64 hex> | None, "preexisting": bool | None}
    images: dict[str, dict[str, Any]]
    # The Podman objects that existed before the run (scripts.podman_dev.inventory).
    inventory: dict[str, Any] | None = None
    # {"inode": ..., "token": ...} of the run directory once the run created it; only that
    # directory is ever removed.
    directory_identity: dict[str, Any] | None = None
    # Unit directories (and their parents) that did not exist before the run, deepest
    # first: the installer creates them, and cleanup removes each one that is empty again.
    created_directories: list[str] = field(default_factory=list)
    installer_started: bool = False
    version: int = 1
    # The rehearsal's images of the previous release, like ``images`` (role -> entry), named
    # before they are built; empty otherwise, and in a record from before the rehearsal.
    previous_images: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def state_home(self) -> Path:
        return Path(self.run_directory) / "state"

    @property
    def root(self) -> Path:
        return Path(self.run_directory) / "root"

    @property
    def temporary(self) -> Path:
        return Path(self.run_directory) / "tmp"

    @property
    def cafile(self) -> Path:
        return Path(self.run_directory) / "caddy-root.pem"

    @property
    def derivatives(self) -> Path:
        """The derivatives directory the installer prepares and the derivatives volume binds:
        an empty directory of the operator's, inside the run's directory, so it goes with it."""
        return Path(self.run_directory) / "derivatives"

    @property
    def previous(self) -> Path:
        """The rehearsal's tree of the previous release, extracted from git."""
        return Path(self.run_directory) / "previous"

    @property
    def previous_installer(self) -> Path:
        return self.previous / "deploy" / "podman" / "aegis-podman"

    @property
    def previous_release(self) -> str:
        return f"{self.release}-previous"


def image_tag(role: str, run: str) -> str:
    return f"localhost/aegis-{role}:deploy-check-{run}"


def previous_tag(role: str, run: str) -> str:
    return f"localhost/aegis-{role}:deploy-check-{run}-previous"


def run_images(record: Record) -> list[dict[str, Any]]:
    """Every image entry of the run: this tree's, then the rehearsal's previous ones."""
    return [*(record.images[role] for role in ROLES), *record.previous_images.values()]


def _image_entry(entry: object, tag: str) -> bool:
    return (
        isinstance(entry, dict) and set(entry) == {"tag", "id", "preexisting"}
        and entry["tag"] == tag
        and (entry["id"] is None
             or (isinstance(entry["id"], str) and re.fullmatch("[0-9a-f]{64}", entry["id"])
                 is not None))
        and entry["preexisting"] in (None, True, False)
    )


def unit_directory_chain(config: Path) -> list[str]:
    """The unit directories and their parents up to the configuration home, deepest first."""
    chain: list[str] = []
    for directory in sorted(set(load_installer().UNIT_DIRECTORIES.values())):
        path = config / directory
        while path != config.parent:
            if str(path) not in chain:
                chain.append(str(path))
            path = path.parent
    return sorted(chain, key=lambda value: (-value.count("/"), value))


def new_record(machine: Machine, run: str, layout: Layout, port: int) -> Record:
    return Record(
        run=run, prefix=checked_prefix(f"aegis-{run}-"), release=f"test-{run[1:]}",
        run_directory=str(state_directory(machine) / run), port=port,
        unit_files=list(layout.unit_files),
        drop_in_directories=list(layout.drop_in_directories), units=list(layout.units),
        containers=sorted(layout.containers.values()), volumes=list(layout.volumes),
        networks=list(layout.networks),
        images={role: {"tag": image_tag(role, run), "id": None, "preexisting": None}
                for role in ROLES},
    )


def _record_problem(machine: Machine, data: object, run: str) -> str | None:
    """What makes ``data`` a record this harness must not act on, or None."""
    fields = {item.name for item in dataclasses.fields(Record)}
    # A record from before the rehearsal has no previous images, and is read as having none.
    if not isinstance(data, dict) or not fields - {"previous_images"} <= set(data) <= fields:
        return "unknown fields"
    if data["version"] != 1 or data["run"] != run or RUN_ID.fullmatch(run) is None:
        return "unknown version or run"
    try:
        checked_prefix(data["prefix"])
    except (HarnessError, TypeError) as error:
        return str(error)
    if data["prefix"] != f"aegis-{run}-" or data["release"] != f"test-{run[1:]}":
        return "prefix or release does not belong to the run"
    if data["run_directory"] != str(state_directory(machine) / run):
        return "the run directory is not the run's own"
    port = data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        return "malformed port"
    prefix = data["prefix"]
    config = config_home(machine)
    unit_directories = {
        str(config / directory) for directory in load_installer().UNIT_DIRECTORIES.values()
    }
    name = re.compile(rf"{re.escape(prefix)}[a-z0-9.-]+")
    for key in ("unit_files", "drop_in_directories"):
        values = data[key]
        if not isinstance(values, list) or not all(
            isinstance(value, str) and os.path.dirname(value) in unit_directories
            and name.fullmatch(os.path.basename(value))
            and (key == "unit_files" or value.endswith(".container.d"))
            for value in values
        ):
            return f"malformed {key}"
    for key in ("units", "containers", "volumes", "networks"):
        values = data[key]
        if not isinstance(values, list) or not all(
            isinstance(value, str) and name.fullmatch(value) for value in values
        ):
            return f"malformed {key}"
    images = data["images"]
    if not isinstance(images, dict) or set(images) != set(ROLES) or not all(
        _image_entry(entry, image_tag(role, run)) for role, entry in images.items()
    ):
        return "malformed images"
    previous = data.get("previous_images", {})
    if not isinstance(previous, dict) or (previous and set(previous) != set(PREVIOUS_ROLES)) \
            or not all(_image_entry(entry, previous_tag(role, run))
                       for role, entry in previous.items()):
        return "malformed previous images"
    if not isinstance(data["inventory"], (dict, type(None))):
        return "malformed inventory"
    identity = data["directory_identity"]
    if identity is not None and not (
        isinstance(identity, dict) and set(identity) == {"inode", "token"}
        and isinstance(identity["inode"], int) and not isinstance(identity["inode"], bool)
        and isinstance(identity["token"], str)
        and re.fullmatch("[0-9a-f]{32}", identity["token"])
    ):
        return "malformed directory identity"
    if not isinstance(data["installer_started"], bool):
        return "malformed installer flag"
    created = data["created_directories"]
    if not isinstance(created, list) or not set(created) <= set(unit_directory_chain(config)):
        return "malformed created directories"
    return None


def state_directory(machine: Machine) -> Path:
    """${XDG_STATE_HOME:-~/.local/state}/aegis-podman-deploy-check: persistent, so a record
    and its run's directory outlive a reboot (/tmp may be a tmpfs). A relative
    XDG_STATE_HOME is invalid and ignored, as the XDG base directory spec says."""
    value = machine.environ.get("XDG_STATE_HOME", "")
    base = Path(os.path.normpath(value)) if os.path.isabs(value) else (
        machine.home / ".local" / "state")
    return base / STATE_DIRECTORY


def record_directory(machine: Machine) -> Path:
    """The state directory, created if missing: private (0700), real and the operator's."""
    directory = state_directory(machine)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != machine.uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise HarnessError(f"{directory} must be a private (0700) directory you own")
    return directory


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[None]:
    """One harness at a time: a record never belongs to a run still in progress."""
    descriptor = os.open(
        directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HarnessError("another podman deploy check is running") from None
        yield
    finally:
        os.close(descriptor)


def save_record(path: Path, record: Record) -> None:
    """Replace the record atomically: a private temporary, fsync, rename."""
    text = json.dumps(dataclasses.asdict(record), indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def load_record(machine: Machine, path: Path, directory: Path) -> Record:
    """A record of this harness's own, refused unless every identity it names is a run's."""
    match = re.fullmatch(r"(t[0-9a-f]{8})\.json", path.name)
    if match is None or Path(os.path.abspath(path)).parent != directory:
        raise HarnessError(f"{path} is not a record in {directory}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise HarnessError(f"{path} does not exist") from None
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != machine.uid
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise HarnessError(f"{path} must be a private (0600) regular file you own")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HarnessError(f"{path} is not readable JSON; nothing was changed") from None
    problem = _record_problem(machine, data, match.group(1))
    if problem is not None:
        raise HarnessError(f"refusing the record {path} ({problem}); nothing was changed")
    return Record(**data)


# -- reporting -----------------------------------------------------------------------------------


def _one_line(text: str, limit: int = 8000) -> str:
    return " ".join(text.split())[:limit]


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in f"{result.stdout or ''}\n{result.stderr or ''}".splitlines()
             if line.strip()]
    return _one_line(lines[-1]) if lines else f"exit {result.returncode}"


def emit(machine: Machine, text: str, *, error: bool = False) -> None:
    """Print one line, best effort, and append it to the run's log.

    A terminal that hung up (EIO) or a reader that went away (EPIPE) must never stop the
    harness, least of all its cleanup: such a line is dropped there and still logged.
    """
    for stream in (machine.err if error else machine.out, machine.log):
        if stream is None:
            continue
        # ValueError: the stream is already closed.
        with contextlib.suppress(OSError, ValueError):
            print(text, file=stream, flush=True)


def open_log(machine: Machine, path: Path) -> None:
    """Append to ``path`` (0600) from now on; without it, output is only printed."""
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        machine.log = os.fdopen(descriptor, "a", encoding="utf-8")
    except OSError as error:
        emit(machine, f"podman_deploy_check: cannot write the log {path}: {error.strerror}",
             error=True)


def close_log(machine: Machine) -> None:
    if machine.log is not None:
        with contextlib.suppress(OSError):
            machine.log.close()
        machine.log = None


class Report:
    def __init__(self, machine: Machine) -> None:
        self.machine = machine
        self.results: dict[str, bool] = {}

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.results[name] = passed
        status = "passed" if passed else "failed"
        line = f"AEGIS_PODMAN_DEPLOY check={name} status={status}"
        emit(self.machine, f"{line} {_one_line(detail)}" if detail else line)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(self.results.values())


def _say(machine: Machine, text: str) -> None:
    emit(machine, f"podman_deploy_check: {text}", error=True)


# -- running programs ------------------------------------------------------------------------------


def _run(
    machine: Machine, arguments: Sequence[str], *, timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a program; a missing program or a timeout reads as a failed run."""
    try:
        if env is None:
            return machine.run(list(arguments), timeout=timeout)
        return machine.run(list(arguments), timeout=timeout, env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(arguments), 127, "", f"{arguments[0]} not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(arguments), 124, "", f"{' '.join(arguments[:4])} did not finish in {timeout:g}s",
        )


def _json(result: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def _entries(value: object) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def installer_environment(environ: Mapping[str, str], record: Record) -> dict[str, str]:
    """The installer's environment: no remote engine, no inherited Python settings, and
    temporary files (aegisctl's observer diagnostics) inside the run's own directory."""
    environment = {
        key: value for key, value in environ.items()
        if key not in REMOTE_VARIABLES and not key.startswith("PYTHON")
    }
    environment["TMPDIR"] = str(record.temporary)
    return environment


def installer_argv(
    machine: Machine, record: Record, command: str, *options: str, installer: Path = INSTALLER,
) -> list[str]:
    return [
        machine.python, str(installer), *command.split(), "--prefix", record.prefix,
        "--state-home", str(record.state_home), *options,
    ]


def run_installer(
    machine: Machine, record: Record, command: str, *options: str, timeout: float,
    installer: Path = INSTALLER,
) -> subprocess.CompletedProcess[str]:
    """This tree's installer, or ``installer`` (the rehearsal's previous one)."""
    return _run(
        machine, installer_argv(machine, record, command, *options, installer=installer),
        timeout=timeout, env=installer_environment(machine.environ, record),
    )


def _show_output(machine: Machine, what: str, result: subprocess.CompletedProcess[str]) -> None:
    """A failed step's own output, which holds no secret, for the operator."""
    lines = f"{result.stdout or ''}\n{result.stderr or ''}".splitlines()
    shown = [line for line in lines if line.strip()][-60:]
    _say(machine, f"{what} failed (exit {result.returncode}); its last output:")
    for line in shown:
        emit(machine, f"  | {line[:500]}", error=True)


def unit_states(machine: Machine, units: Sequence[str]) -> dict[str, dict[str, str]]:
    result = _run(
        machine,
        [*SYSTEMCTL, "show", "--property=Id,LoadState,ActiveState,SubState,Result,NRestarts,Job",
         *units],
        timeout=60,
    )
    if result.returncode != 0:
        raise CheckFailed(f"systemctl --user show failed: {_last_line(result)}")
    return parse_unit_states(result.stdout, units)


def parse_unit_states(text: str, units: Sequence[str]) -> dict[str, dict[str, str]]:
    """``systemctl show`` blocks by unit; a unit systemd did not describe is not-found."""
    found: dict[str, dict[str, str]] = {}
    for block in text.split("\n\n"):
        values = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
        if "Id" in values:
            found[values["Id"]] = {"Job": ""} | values
    missing = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
               "Result": "success", "NRestarts": "0", "Job": ""}
    return {unit: found.get(unit, {"Id": unit, **missing}) for unit in units}


def inspect_container(machine: Machine, name: str) -> dict[str, Any] | None:
    result = _run(machine, [*PODMAN, "container", "inspect", name], timeout=60)
    entries = _entries(_json(result)) if result.returncode == 0 else []
    return entries[0] if len(entries) == 1 and entries[0].get("Name") == name else None


def health_status(entry: Mapping[str, Any] | None) -> str:
    if entry is None:
        return "absent"
    state = entry.get("State") or {}
    health = state.get("Health") or state.get("Healthcheck") or {}
    return str(health.get("Status") or "none")


def _exists(machine: Machine, kind: str, name: str) -> bool | None:
    """Whether a Podman object exists: True, False, or None when Podman could not tell."""
    result = _run(machine, [*PODMAN, kind, "exists", name], timeout=60)
    return {0: True, 1: False}.get(result.returncode)


# -- preflight --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    port: int
    address: str
    inventory: dict[str, Any]


def systemd_version(text: str) -> int | None:
    match = re.match(r"systemd (\d+)\b", text)
    return int(match.group(1)) if match else None


def mapped_ports(containers: Iterable[Mapping[str, Any]]) -> set[int]:
    """Every host port any container maps, ranges included."""
    ports: set[int] = set()
    for container in containers:
        for mapping in container.get("Ports") or []:
            try:
                first = int(mapping["host_port"])
                count = max(int(mapping.get("range") or 1), 1)
            except (KeyError, TypeError, ValueError):
                continue
            ports.update(range(first, first + count))
    return ports


def pick_port(machine: Machine, mapped: set[int]) -> int | None:
    for candidate in machine.port_candidates():
        if candidate in RESERVED_PORTS or candidate in mapped:
            continue
        if not machine.port_in_use(candidate):
            return candidate
    return None


def preferred_source(text: str) -> str | None:
    """The preferred source address ``ip -j route get`` reports, when it is a usable IPv4."""
    try:
        routes = _entries(json.loads(text or "null"))
        address = ipaddress.IPv4Address(routes[0]["prefsrc"]) if routes else None
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if address is None or address.is_loopback or address.is_unspecified \
            or address.is_link_local or address.is_multicast:
        return None
    return str(address)


def other_runs_files(layout: Layout) -> list[str]:
    """Unit files and drop-in directories of other deploy-check runs, such as one whose
    record was lost; they are only reported."""
    own = {*layout.unit_files, *layout.drop_in_directories}
    found = []
    for directory in sorted({os.path.dirname(path) for path in layout.unit_files}):
        with contextlib.suppress(OSError):
            found += [entry.path for entry in os.scandir(directory)
                      if OTHER_RUN.match(entry.name) and entry.path not in own]
    return sorted(found)


def preflight(machine: Machine, layout: Layout, run_directory: Path) -> tuple[list[str], Facts]:
    """Every reason not to start, gathered; nothing is created."""
    problems: list[str] = []
    info = _mapping(_json(_run(machine, [*PODMAN, "info", "--format", "json"], timeout=120)))
    security = _mapping(_mapping(info.get("host")).get("security"))
    graph_root = _mapping(info.get("store")).get("graphRoot")
    if security.get("rootless") is not True:
        problems.append("Podman must run rootless")
    if security.get("selinuxEnabled") is not True:
        problems.append("SELinux must be enabled for Podman")
    try:
        enforcing = machine.selinux_enforce.read_text(encoding="ascii").strip() == "1"
    except OSError:
        enforcing = False
    if not enforcing:
        problems.append("SELinux must be enforcing")
    version = systemd_version(_run(machine, ["systemctl", "--version"], timeout=60).stdout or "")
    if version is None or version < MINIMUM_SYSTEMD:
        problems.append(f"systemd {MINIMUM_SYSTEMD} or newer is needed (found {version})")
    for reserved in RESERVED_PORTS:
        if machine.port_in_use(reserved):
            problems.append(f"port {reserved} has a listener: another test stack may be running")
    listing = _run(machine, [*PODMAN, "ps", "-a", "--format", "json"], timeout=120)
    if listing.returncode != 0:
        problems.append(f"podman ps failed: {_last_line(listing)}")
    port = pick_port(machine, mapped_ports(_entries(_json(listing))))
    if port is None:
        problems.append(f"no free test port in {TEST_PORTS.start}-{TEST_PORTS.stop - 1}")
    if not isinstance(graph_root, str) or not os.path.isabs(graph_root):
        problems.append("podman info names no graph root")
    else:
        try:
            available = machine.free_bytes(graph_root)
        except OSError as error:
            problems.append(f"cannot read the free space of the graph root: {error.strerror}")
        else:
            if available < MINIMUM_FREE_BYTES:
                problems.append(f"the graph root's file system has {available / 2**30:.1f} GiB "
                                f"free; at least {MINIMUM_FREE_BYTES // 2**30} GiB are needed")
    route = _run(machine, ["ip", "-j", "route", "get", ROUTE_PROBE], timeout=30)
    address = preferred_source(route.stdout) if route.returncode == 0 else None
    if address is None:
        problems.append("no primary non-loopback IPv4 address (ip route get names no usable "
                        "preferred source)")
    existing = [path for path in (*layout.unit_files, *layout.drop_in_directories)
                if os.path.lexists(path)]
    if existing:
        problems.append(f"files the run would write already exist: {', '.join(existing)}")
    others = other_runs_files(layout)
    if others:
        _say(machine, "warning: unit files of another deploy-check run, never removed by this "
                      f"one: {', '.join(others)}")
    if os.path.lexists(run_directory):
        problems.append(f"{run_directory} already exists")
    secrets_directory = run_directory / "state" / "secrets"
    if load_installer().SECRETS_DIR.fullmatch(str(secrets_directory)) is None:
        problems.append(f"the installer cannot use {secrets_directory}: give XDG_STATE_HOME a "
                        "path of letters, digits and ._@+- only")
    if not INSTALLER.is_file():
        problems.append(f"{INSTALLER} is missing")
    snapshot: dict[str, Any] = {}
    try:
        snapshot = inventory.snapshot(
            lambda arguments: machine.run([*PODMAN, *arguments], timeout=120),
        )
    except Exception as error:
        problems.append(f"cannot take the Podman inventory: {type(error).__name__}: {error}")
    return problems, Facts(port or 0, address or "", snapshot)


# -- images ------------------------------------------------------------------------------------


def inspect_image(machine: Machine, reference: str) -> dict[str, Any] | None:
    result = _run(machine, [*PODMAN, "image", "inspect", reference], timeout=120)
    entries = _entries(_json(result)) if result.returncode == 0 else []
    return entries[0] if len(entries) == 1 else None


def _labels(entry: Mapping[str, Any]) -> dict[str, Any]:
    labels = entry.get("Labels") or (entry.get("Config") or {}).get("Labels") or {}
    return labels if isinstance(labels, dict) else {}


def _build(
    machine: Machine, record: Record, record_path: Path, images: Mapping[str, dict[str, Any]],
    context: Path,
) -> str | None:
    """Build each of ``images`` (role -> entry, named in the record) from ``context`` and its
    docker/<role>.Dockerfile, tagged and labelled for the run; the failure, or None."""
    before = set((record.inventory or {}).get("images", {}))
    for role, image in images.items():
        _say(machine, f"building the {role} image ({image['tag']})")
        started = machine.monotonic()
        result = _run(
            machine,
            [*GENTLE, *PODMAN, "build", "--file", str(context / "docker" / f"{role}.Dockerfile"),
             "--label", f"{RUN_LABEL}={record.run}", "--tag", image["tag"], str(context)],
            timeout=BUILD_TIMEOUT,
        )
        entry = inspect_image(machine, image["tag"])
        if entry is not None and _labels(entry).get(RUN_LABEL) == record.run and \
                re.fullmatch("[0-9a-f]{64}", str(entry.get("Id"))):
            image["id"] = entry["Id"]
            image["preexisting"] = entry["Id"] in before
            save_record(record_path, record)
        if result.returncode != 0:
            _show_output(machine, f"podman build of {role}", result)
            return f"{role}: podman build failed: {_last_line(result)}"
        if image["id"] is None:
            return f"{role}: the built tag is not this run's image"
        _say(machine, f"built {role} in {machine.monotonic() - started:.0f}s")
    return None


def build_images(machine: Machine, record: Record, record_path: Path, report: Report) -> bool:
    """Build the five images from the working tree, each tagged and labelled for the run."""
    problem = _build(machine, record, record_path, {role: record.images[role] for role in ROLES},
                     REPOSITORY)
    report.check("build_images", problem is None,
                 problem or f"{len(ROLES)} images tagged deploy-check-{record.run}")
    return problem is None


# -- installing the test stack -------------------------------------------------------------------


def make_synthetic_root(machine: Machine, record: Record) -> None:
    """A fresh fixture, never an original: group-only (0750/0640), the operator's, labelled
    container_file_t."""
    root = record.root
    album = root / "album"
    files = {root / ROOT_FILE: f"aegis podman deploy check {record.run}\n",
             album / "photo.txt": f"aegis podman deploy check {record.run}: nested\n"}
    for directory in (root, album):
        directory.mkdir(mode=0o750)
        directory.chmod(0o750)
    for path, text in files.items():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            os.fchmod(descriptor, 0o640)
            os.write(descriptor, text.encode("ascii"))
        finally:
            os.close(descriptor)
    for path in (root, album, *files):
        result = _run(machine, ["chcon", "-h", "-t", CONTAINER_FILE_TYPE, str(path)], timeout=60)
        if result.returncode != 0:
            raise HarnessError(f"labelling the synthetic root failed: {_last_line(result)}")


def _install(
    machine: Machine, record: Record, address: str, *, installer: Path, release: str,
    images: str, options: tuple[str, ...] = (),
) -> str | None:
    """init (with ``options``), install, the synthetic root and roots add, then start, each
    through ``installer``'s CLI; the failure, or None."""
    steps: list[tuple[str, tuple[str, ...], float]] = [
        ("init", ("--release", release, "--https-port", str(record.port),
                  "--name", LOOPBACK, "--name", address, "--local-images", images,
                  "--reader-type", TEST_READER_TYPE, *options), 900),
        ("install", ("--no-install-sections",), 300),
        ("roots add", ("--slot", SLOT, "--source", str(record.root), "--timeout", "600"), 900),
        ("start", ("--timeout", str(START_TIMEOUT)), START_TIMEOUT + 300),
    ]
    which = "aegis-podman" if installer == INSTALLER else "the previous aegis-podman"
    for command, step_options, timeout in steps:
        if command == "roots add":
            try:
                make_synthetic_root(machine, record)
            except (HarnessError, OSError) as error:
                return f"synthetic root: {error}"
        _say(machine, f"{which} {command}")
        result = run_installer(machine, record, command, *step_options, timeout=timeout,
                               installer=installer)
        if result.returncode != 0:
            _show_output(machine, f"{which} {command}", result)
            return f"{command}: {_last_line(result)}"
    return None


def install_stack(
    machine: Machine, record: Record, record_path: Path, address: str, report: Report,
) -> bool:
    """init with the run's derivatives directory, install, the synthetic root and start,
    each through the installer CLI."""
    record.installer_started = True
    save_record(record_path, record)
    images = ",".join(f"{role}={record.images[role]['id']}" for role in ROLES)
    problem = _install(machine, record, address, installer=INSTALLER, release=record.release,
                       images=images, options=("--derivatives-dir", str(record.derivatives)))
    report.check("install", problem is None, problem or (
        "init with a derivatives directory, install --no-install-sections, roots add and "
        "start through aegis-podman"))
    return problem is None


# -- checks ------------------------------------------------------------------------------------


@dataclass
class Context:
    machine: Machine
    record: Record
    layout: Layout
    address: str
    cafile: Path | None = None


def evaluate_units(
    layout: Layout, states: Mapping[str, Mapping[str, str]],
    containers: Mapping[str, Mapping[str, Any] | None],
) -> list[str]:
    """What keeps the stack from being up: each long-running service active and running,
    migrate active and exited, the socket active, and every health check healthy. systemd
    reports the socket as running while the service it hands its connections to runs."""
    problems = []
    expected = [
        *((unit, {"running"}) for unit in layout.services),
        *((unit, {"exited"}) for unit in layout.oneshots),
        (layout.socket, {"listening", "running"}),
    ]
    for unit, subs in expected:
        info = states.get(unit, {})
        if info.get("ActiveState") != "active" or info.get("SubState") not in subs:
            problems.append(f"{unit} is {info.get('ActiveState')}/{info.get('SubState')}")
    for unit in layout.health_checked:
        status = health_status(containers.get(unit))
        if status != "healthy":
            problems.append(f"{unit} health is {status}")
    return problems


def unit_problems(context: Context) -> list[str]:
    layout = context.layout
    states = unit_states(context.machine, (layout.socket, *layout.services, *layout.oneshots))
    containers = {
        unit: inspect_container(context.machine, layout.containers[unit])
        for unit in layout.health_checked
    }
    return evaluate_units(layout, states, containers)


def check_units_healthy(context: Context) -> str:
    problems = unit_problems(context)
    if problems:
        raise CheckFailed("; ".join(problems))
    layout = context.layout
    return (f"{len(layout.services)} services active and running, "
            f"{len(layout.health_checked)} healthy, migrate completed, socket active")


def install_sections(paths: Iterable[str]) -> list[str]:
    """The unit files and drop-ins (in drop-in directories) with an [Install] section."""
    found = []
    for path in paths:
        files = sorted(Path(path).glob("*.conf")) if path.endswith(".d") else [Path(path)]
        for file in files:
            if any(line.strip() == "[Install]" for line in file.read_text("utf-8").splitlines()):
                found.append(file.name)
    return found


def enablement_links(machine: Machine, config: Path, units: Iterable[str]) -> list[str]:
    """Links that would pull one of ``units`` in at boot: in the user's unit directory, the
    runtime one, and every generator output directory."""
    names = set(units)
    runtime = Path(machine.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "systemd"
    roots = [config / "systemd" / "user", runtime / "user", runtime / "generator",
             runtime / "generator.early", runtime / "generator.late"]
    found = []
    for root in roots:
        with contextlib.suppress(OSError):
            for directory in os.scandir(root):
                if directory.name.endswith((".wants", ".requires", ".upholds")):
                    found += [f"{directory.name}/{entry.name}"
                              for entry in os.scandir(directory.path) if entry.name in names]
    return sorted(found)


def check_not_boot_enabled(context: Context) -> str:
    layout, machine = context.layout, context.machine
    missing = [path for path in layout.unit_files if not os.path.isfile(path)]
    problems = [f"{os.path.basename(path)} is missing" for path in missing]
    present = [path for path in (*layout.unit_files, *layout.drop_in_directories)
               if path not in missing]
    problems += [f"{name} has an [Install] section" for name in install_sections(present)]
    result = _run(machine, [*SYSTEMCTL, "is-enabled", *layout.units], timeout=60)
    states = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(states) != len(layout.units):
        problems.append(f"systemctl is-enabled described {len(states)} of {len(layout.units)} "
                        "units")
    problems += [f"{unit} is {state}" for unit, state in zip(layout.units, states, strict=False)
                 if state in BOOT_STATES]
    links = enablement_links(machine, config_home(machine), layout.units)
    problems += [f"link {link}" for link in links]
    if problems:
        raise CheckFailed("; ".join(problems))
    seen = ", ".join(sorted(set(states)))
    return (f"no [Install] section in {len(layout.unit_files)} unit files or the drop-ins; "
            f"is-enabled: {seen}; no .wants or .requires link")


def check_no_restart_policy(context: Context) -> str:
    problems = []
    inspected = 0
    for service, name in sorted(context.layout.containers.items()):
        entry = inspect_container(context.machine, name)
        if entry is None:
            if service in context.layout.services:
                problems.append(f"{name} is missing")
            continue
        inspected += 1
        policy = (entry.get("HostConfig") or {}).get("RestartPolicy") or {}
        if policy.get("Name", "") not in ("", "no"):
            problems.append(f"{name} has the restart policy {policy.get('Name')}")
    if problems:
        raise CheckFailed("; ".join(problems))
    return f"{inspected} containers inspected; none has a restart policy"


def published_ports(entry: Mapping[str, Any]) -> list[str]:
    bindings = (entry.get("HostConfig") or {}).get("PortBindings") or {}
    ports = (entry.get("NetworkSettings") or {}).get("Ports") or {}
    return sorted({key for key, value in [*bindings.items(), *ports.items()] if value})


def check_gateway_unpublished(context: Context) -> str:
    name = context.layout.containers[f"{context.record.prefix}gateway.service"]
    entry = inspect_container(context.machine, name)
    if entry is None:
        raise CheckFailed(f"{name} is missing")
    published = published_ports(entry)
    if published:
        raise CheckFailed(f"{name} publishes {', '.join(published)}")
    return f"{name} has no host port binding"


def _until(machine: Machine, deadline: float, attempt: Callable[[], Any]) -> Any:
    """Retry ``attempt`` until it returns without raising, or re-raise at the deadline."""
    end = machine.monotonic() + deadline
    while True:
        try:
            return attempt()
        except Exception:
            if machine.monotonic() >= end:
                raise
            machine.sleep(POLL_SECONDS)


def exported_ca(context: Context) -> Path:
    """Caddy's local root, through aegis-podman ca-cert, in a private file of the run's."""
    if context.cafile is not None:
        return context.cafile
    machine, record = context.machine, context.record

    def export() -> str:
        result = run_installer(machine, record, "ca-cert", timeout=120)
        if result.returncode != 0 or not result.stdout.startswith("-----BEGIN CERTIFICATE-----"):
            raise CheckFailed(f"aegis-podman ca-cert failed: {_last_line(result)}")
        return result.stdout

    pem = _until(machine, CA_DEADLINE, export)
    descriptor = os.open(record.cafile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    try:
        os.write(descriptor, pem.encode("ascii"))
    finally:
        os.close(descriptor)
    context.cafile = record.cafile
    return record.cafile


def check_tls_local_ca(context: Context) -> str:
    cafile = exported_ca(context)
    machine, port = context.machine, context.record.port
    try:
        response = _until(machine, HTTPS_DEADLINE, lambda: machine.request(
            LOOPBACK, port, "GET", CSRF_PATH, cafile,
        ))
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        raise CheckFailed(f"GET {CSRF_PATH} failed: {type(error).__name__}: {error}") from None
    if response.status != 200:
        raise CheckFailed(f"GET {CSRF_PATH} answered {response.status}")
    return (f"GET https://{LOOPBACK}:<port>{CSRF_PATH} verified with the exported local root: "
            "200")


def _login(context: Context, address: str, cafile: Path) -> int:
    """One login POST without a CSRF token, from ``address`` (the kernel's choice of source
    for a connection to that address of this host)."""
    response = context.machine.request(
        address, context.record.port, "POST", LOGIN_PATH, cafile, body=b"{}",
    )
    if response.source != address:
        raise CheckFailed(f"a connection to {address} left from {response.source}")
    return response.status


def _drain(context: Context, address: str, cafile: Path) -> tuple[int, int]:
    """POST until the gateway limits ``address``: the number of requests and the status."""
    for count in range(1, THROTTLE_REQUESTS + 1):
        status = _login(context, address, cafile)
        if status in LIMITED:
            return count, status
        if status >= 500:
            raise CheckFailed(f"a login POST answered {status}")
    raise CheckFailed(f"{THROTTLE_REQUESTS} login POSTs were never rate-limited")


def check_throttle_per_address(context: Context) -> str:
    """O11: loopback and the host's address are different clients to the gateway."""
    cafile = exported_ca(context)
    names = {LOOPBACK: "loopback", context.address: "the host address"}
    sequence = [(LOOPBACK, context.address), (context.address, LOOPBACK)]
    details = []
    try:
        for index, (drained, other) in enumerate(sequence):
            if index:
                context.machine.sleep(THROTTLE_COOLDOWN)
            count, limited = _drain(context, drained, cafile)
            status = _login(context, other, cafile)
            if status in LIMITED:
                raise CheckFailed(f"{names[other]} was limited ({status}) right after "
                                  f"{names[drained]} was: one bucket for both")
            details.append(f"{names[drained]} limited ({limited}) after {count} POSTs, then "
                           f"{names[other]} answered {status}")
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        raise CheckFailed(f"a login POST failed: {type(error).__name__}: {error}") from None
    return "; ".join(details)


def check_root_read_only(context: Context) -> str:
    name = context.layout.containers[f"{context.record.prefix}indexer.service"]
    result = _run(
        context.machine,
        [*PODMAN, "exec", name, "python3", "-I", "-c", ROOT_PROBE, SLOT_TARGET, ROOT_FILE,
         WRITE_PROBE],
        timeout=120,
    )
    if result.returncode != 0:
        raise CheckFailed(f"reading the root in {name} failed: {_last_line(result)}")
    lines = (result.stdout or "").strip().splitlines()
    try:
        seen = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        seen = None
    if not isinstance(seen, dict):
        raise CheckFailed("the root probe printed no result")
    problems = []
    if seen.get("entries") != list(ROOT_ENTRIES):
        problems.append(f"listed {seen.get('entries')}")
    if seen.get("read") != f"aegis podman deploy check {context.record.run}\n":
        problems.append("read other content")
    if seen.get("write") != "EROFS":
        problems.append(f"the write attempt gave {seen.get('write')}, not EROFS")
    if problems:
        raise CheckFailed("; ".join(problems))
    return (f"{name} listed {SLOT_TARGET} and read {ROOT_FILE} through the group mapping; "
            "creating a file failed with EROFS")


def check_media_processor_isolated(context: Context) -> str:
    """Spec M2: the processor runs with no network, no secret, the root bound read-only and
    no way to gain privileges, as Podman reports it and as the container itself sees it."""
    machine = context.machine
    name = context.layout.containers[f"{context.record.prefix}media-processor.service"]
    entry = inspect_container(machine, name)
    if entry is None:
        raise CheckFailed(f"{name} is missing")
    problems = []
    mode = (entry.get("HostConfig") or {}).get("NetworkMode")
    if mode != "none":
        problems.append(f"Podman reports the network mode {mode}, not none")
    binds = [mount for mount in entry.get("Mounts") or []
             if isinstance(mount, dict) and mount.get("Destination") == SLOT_TARGET]
    if len(binds) != 1 or binds[0].get("RW") is not False:
        problems.append(f"Podman does not report {SLOT_TARGET} as one read-only bind")
    result = _run(
        machine,
        [*PODMAN, "exec", name, "python3", "-I", "-c", PROCESSOR_PROBE, SLOT_TARGET,
         WRITE_PROBE],
        timeout=120,
    )
    if result.returncode != 0:
        raise CheckFailed(f"probing {name} failed: {_last_line(result)}")
    lines = (result.stdout or "").strip().splitlines()
    try:
        seen = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        seen = None
    if not isinstance(seen, dict):
        raise CheckFailed("the processor probe printed no result")
    if seen.get("interfaces") != ["lo"]:
        problems.append(f"/proc/net/dev lists {seen.get('interfaces')}, not only lo")
    secrets = seen.get("secrets")
    if secrets != []:
        # A count only: even the names stay out of the report.
        count = len(secrets) if isinstance(secrets, list) else "an unknown number of"
        problems.append(f"{count} entries under /run/secrets")
    if seen.get("write") != "EROFS":
        problems.append(f"creating a file in the root gave {seen.get('write')}, not EROFS")
    if seen.get("no_new_privs") != "1":
        problems.append(f"NoNewPrivs is {seen.get('no_new_privs')}, not 1")
    if problems:
        raise CheckFailed("; ".join(problems))
    return (f"{name}: network mode none and only lo in /proc/net/dev, nothing under "
            f"/run/secrets, {SLOT_TARGET} read-only (EROFS), NoNewPrivs 1")


def _mcs_level(entry: Mapping[str, Any]) -> str | None:
    """The MCS level of a container's process label (user:role:type:level), or None."""
    label = entry.get("ProcessLabel")
    parts = label.split(":", 3) if isinstance(label, str) else []
    return parts[3] if len(parts) == 4 and parts[3] else None


def check_shared_volume_exchange(context: Context) -> str:
    """One MCS level for the install: a file the media worker creates in scratch is readable
    by the processor, and one it writes into the derivatives is readable by the gateway (a
    file carries its creator's level, so separate levels would refuse both)."""
    machine, record = context.machine, context.record
    names = {role: context.layout.containers[f"{record.prefix}{role}.service"]
             for role in ("media", "media-processor", "gateway")}
    levels = {}
    for role, name in names.items():
        entry = inspect_container(machine, name)
        if entry is None:
            raise CheckFailed(f"{name} is missing")
        levels[role] = _mcs_level(entry)
    problems = []
    if None in levels.values() or len(set(levels.values())) != 1:
        problems.append("the SELinux levels differ: " + ", ".join(
            f"{names[role]} {level}" for role, level in levels.items()))
    text = f"aegis podman deploy check {record.run}: exchange"
    try:
        for path in (EXCHANGE_SCRATCH, EXCHANGE_DERIVATIVE):
            written = _run(machine, [*PODMAN, "exec", names["media"], "python3", "-I", "-c",
                                     WRITE_EXCHANGE, path, text], timeout=120)
            if written.returncode != 0:
                # Reported with whatever the removal below reports: a file written before
                # this one may be left.
                problems.append(f"{names['media']} could not write {path}: "
                                f"{_last_line(written)}")
                break
        else:
            for role, path in (("media-processor", EXCHANGE_SCRATCH),
                               ("gateway", EXCHANGE_DERIVATIVE)):
                read = _run(machine, [*PODMAN, "exec", names[role], "cat", path], timeout=120)
                if read.returncode != 0:
                    problems.append(f"{names[role]} could not read {path}: {_last_line(read)}")
                elif read.stdout != text:
                    problems.append(f"{names[role]} read other content from {path}")
    finally:
        removed = _run(machine, [*PODMAN, "exec", names["media"], "python3", "-I", "-c",
                                 REMOVE_EXCHANGE, EXCHANGE_SCRATCH, EXCHANGE_DERIVATIVE],
                       timeout=120)
    if removed.returncode != 0:
        problems.append(f"the exchange files were not removed: {_last_line(removed)}")
    if problems:
        raise CheckFailed("; ".join(problems))
    return (f"one SELinux level ({levels['media']}); {names['media-processor']} read the "
            f"worker's file in scratch and {names['gateway']} its file in the derivatives")


def _systemctl(machine: Machine, *arguments: str) -> None:
    result = _run(machine, [*SYSTEMCTL, *arguments], timeout=120)
    if result.returncode != 0:
        raise CheckFailed(f"systemctl --user {' '.join(arguments)} failed: {_last_line(result)}")


def check_postgres_restart_recovers(context: Context) -> str:
    machine = context.machine
    service = f"{context.record.prefix}postgres.service"
    name = context.layout.containers[service]
    before = (inspect_container(machine, name) or {}).get("Id")
    started = machine.monotonic()
    _systemctl(machine, "--no-block", "restart", service)
    end = started + RECOVERY_DEADLINE
    while True:
        after = (inspect_container(machine, name) or {}).get("Id")
        problems = unit_problems(context) if after and after != before else [
            f"{name} was not recreated yet",
        ]
        if not problems:
            return (f"systemctl --user restart {service}: every unit active and healthy again "
                    f"after {machine.monotonic() - started:.0f}s, without manual steps")
        if machine.monotonic() >= end:
            raise CheckFailed(f"not recovered within {RECOVERY_DEADLINE:g}s: "
                              + "; ".join(problems))
        machine.sleep(POLL_SECONDS)


def check_migrate_failure_recovers(context: Context) -> str:
    """Migrate fails without PostgreSQL and retries in place (RestartMode=direct); the
    dependents queued meanwhile then start once it succeeds (spec D13)."""
    machine, record = context.machine, context.record
    postgres = f"{record.prefix}postgres.service"
    migrate = f"{record.prefix}migrate.service"
    _systemctl(machine, "--no-block", "stop", postgres)
    end = machine.monotonic() + STOP_DEADLINE
    while True:
        states = unit_states(machine, (postgres, migrate))
        if all(info["ActiveState"] in ("inactive", "failed") and not info["Job"]
               for info in states.values()):
            break
        if machine.monotonic() >= end:
            raise CheckFailed(f"PostgreSQL and migrate did not stop within {STOP_DEADLINE:g}s")
        machine.sleep(POLL_SECONDS)
    # Requires= would start PostgreSQL along with migrate; this one start ignores it.
    _systemctl(machine, "--no-block", "--job-mode=ignore-requirements", "start", migrate)
    end = machine.monotonic() + MIGRATE_FAILURE_DEADLINE
    while True:
        info = unit_states(machine, (migrate,))[migrate]
        restarts = int(info["NRestarts"]) if info.get("NRestarts", "").isdigit() else 0
        # systemd 254 also reports auto-restart-queued while the retry job waits.
        if info["SubState"].startswith("auto-restart") or (
            restarts >= 1 and info["ActiveState"] == "activating"
        ):
            break
        if (info["ActiveState"], info["SubState"]) == ("active", "exited"):
            raise CheckFailed("migrate completed without PostgreSQL")
        if info["ActiveState"] == "failed":
            raise CheckFailed("migrate failed for good instead of retrying in place")
        if machine.monotonic() >= end:
            raise CheckFailed(f"migrate showed no failed attempt within "
                              f"{MIGRATE_FAILURE_DEADLINE:g}s ({info['ActiveState']}/"
                              f"{info['SubState']})")
        machine.sleep(POLL_SECONDS)
    # PostgreSQL and every dependent are queued while migrate is still retrying.
    result = run_installer(machine, record, "start", "--timeout", str(START_TIMEOUT),
                           timeout=START_TIMEOUT + 300)
    if result.returncode != 0:
        _show_output(machine, "aegis-podman start after the migrate failure", result)
        raise CheckFailed(f"aegis-podman start: {_last_line(result)}")
    problems = unit_problems(context)
    if problems:
        raise CheckFailed("; ".join(problems))
    return ("migrate failed without PostgreSQL and retried in place; once aegis-podman start "
            "queued PostgreSQL and the dependents, migrate completed and every unit became "
            "active and healthy")


CHECK_FUNCTIONS: dict[str, Callable[[Context], str]] = {
    "units_healthy": check_units_healthy,
    "not_boot_enabled": check_not_boot_enabled,
    "no_restart_policy": check_no_restart_policy,
    "gateway_unpublished": check_gateway_unpublished,
    "tls_local_ca": check_tls_local_ca,
    "throttle_per_address": check_throttle_per_address,
    "root_read_only": check_root_read_only,
    "media_processor_isolated": check_media_processor_isolated,
    "shared_volume_exchange": check_shared_volume_exchange,
    "postgres_restart_recovers": check_postgres_restart_recovers,
    "migrate_failure_recovers": check_migrate_failure_recovers,
}


def run_checks(context: Context, report: Report) -> None:
    for name in CHECKS:
        try:
            detail = CHECK_FUNCTIONS[name](context)
        except (CheckFailed, HarnessError) as error:
            report.check(name, False, str(error))
        except (OSError, ValueError, KeyError, TypeError) as error:
            report.check(name, False, f"{type(error).__name__}: {error}")
        else:
            report.check(name, True, detail)


# -- the level migration rehearsal ---------------------------------------------------------


@dataclass
class Rehearsal:
    """--rehearse-level-migration: what its steps share."""

    machine: Machine
    record: Record
    record_path: Path
    layout: Layout
    address: str
    commit: str
    revision: str = ""  # the full commit ID, once resolved
    marker: str | None = None  # the marker administrator's user ID
    level: str | None = None  # the shared level the upgrade recorded


def _git(
    machine: Machine, *arguments: str, timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return _run(machine, ["git", "-C", str(REPOSITORY), *arguments], timeout=timeout)


def _previous_tree(rehearsal: Rehearsal) -> None:
    """The commit's tracked tree, extracted into the run's directory with ``git archive``;
    its installer must be one from before the shared level."""
    machine, record = rehearsal.machine, rehearsal.record
    resolved = _git(machine, "rev-parse", "--verify", "--quiet", f"{rehearsal.commit}^{{commit}}")
    revision = (resolved.stdout or "").strip()
    if resolved.returncode != 0 or re.fullmatch("[0-9a-f]{40}", revision) is None:
        raise CheckFailed(f"{rehearsal.commit} names no commit of {REPOSITORY}")
    rehearsal.revision = revision
    archive = Path(record.run_directory) / "previous.tar"
    exported = _git(machine, "archive", "--format=tar", f"--output={archive}", revision,
                    timeout=600)
    if exported.returncode != 0:
        raise CheckFailed(f"git archive {revision[:12]} failed: {_last_line(exported)}")
    record.previous.mkdir(mode=0o700)
    unpacked = _run(machine, ["tar", "-x", "-f", str(archive), "-C", str(record.previous)],
                    timeout=600)
    if unpacked.returncode != 0:
        raise CheckFailed(f"extracting {archive} failed: {_last_line(unpacked)}")
    try:
        text = record.previous_installer.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CheckFailed(f"commit {revision[:12]} has no readable deploy/podman/aegis-podman"
                          ) from None
    api = re.search(r"^RENDER_API = (\d+)$", text, re.MULTILINE)
    roles = re.search(r"^ROLES = \(([^)]*)\)$", text, re.MULTILINE)
    if api is None or api.group(1) != "1":
        raise CheckFailed(
            f"the installer at {revision[:12]} renders API {api.group(1) if api else 'unknown'}"
            ", not 1, so it already shares a level: give --from-commit a commit from before "
            "the shared SELinux level"
        )
    if roles is None or tuple(re.findall(r'"([a-z-]+)"', roles.group(1))) != PREVIOUS_ROLES:
        raise CheckFailed(f"the installer at {revision[:12]} does not have the images "
                          f"{', '.join(PREVIOUS_ROLES)}")


def rehearse_previous_install(rehearsal: Rehearsal) -> str:
    """The previous release, built from its commit and installed and started by its own
    installer, which runs every container at a random SELinux level."""
    machine, record = rehearsal.machine, rehearsal.record
    _previous_tree(rehearsal)
    # Named in the record before they exist, as the others are.
    record.previous_images = {
        role: {"tag": previous_tag(role, record.run), "id": None, "preexisting": None}
        for role in PREVIOUS_ROLES
    }
    save_record(rehearsal.record_path, record)
    problem = _build(machine, record, rehearsal.record_path, record.previous_images,
                     record.previous)
    if problem is not None:
        raise CheckFailed(problem)
    images = ",".join(f"{role}={record.previous_images[role]['id']}" for role in PREVIOUS_ROLES)
    problem = _install(machine, record, rehearsal.address, installer=record.previous_installer,
                       release=record.previous_release, images=images)
    if problem is not None:
        raise CheckFailed(f"the previous aegis-podman {problem}")
    return (f"commit {rehearsal.revision[:12]} (render API 1): {len(PREVIOUS_ROLES)} images "
            "built, then init, install, roots add and start through its own aegis-podman")


def _bootstrap(rehearsal: Rehearsal, installer: Path) -> tuple[str, bool]:
    """bootstrap-password for the marker administrator: its user ID, and whether this call
    created it. The first call prints the generated password on standard output, so none of
    that output is ever shown or logged; only the user_id line is read from it."""
    result = run_installer(rehearsal.machine, rehearsal.record, "bootstrap-password",
                           "--username", MARKER_USER, "--email", MARKER_EMAIL, timeout=900,
                           installer=installer)
    if result.returncode != 0:
        errors = subprocess.CompletedProcess(result.args, result.returncode, "", result.stderr)
        raise CheckFailed(f"bootstrap-password: {_last_line(errors)}")
    found = [match for line in (result.stdout or "").splitlines()
             if (match := BOOTSTRAPPED.fullmatch(line.strip())) is not None]
    if len(found) != 1:
        raise CheckFailed("bootstrap-password printed no single user_id=<uuid> created=<bool> line")
    return found[0].group(1), found[0].group(2) == "true"


def rehearse_marker(rehearsal: Rehearsal) -> str:
    """A PostgreSQL write through the previous release: the marker administrator."""
    user_id, created = _bootstrap(rehearsal, rehearsal.record.previous_installer)
    if not created:
        raise CheckFailed(f"the marker administrator {user_id} existed before the rehearsal")
    rehearsal.marker = user_id
    return f"bootstrap-password through the previous release created administrator {user_id}"


def _installer_state(rehearsal: Rehearsal) -> dict[str, Any]:
    path = rehearsal.record.state_home / "state.json"
    try:
        data = json.loads(_read_private(path, rehearsal.machine.uid))
    except (OSError, HarnessError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckFailed(f"cannot read the installer's record {path}: {error}") from None
    if not isinstance(data, dict) or data.get("prefix") != rehearsal.record.prefix:
        raise CheckFailed(f"{path} is not the installer's record for {rehearsal.record.prefix}")
    return data


def rehearse_upgrade(rehearsal: Rehearsal) -> str:
    """Stop through the previous installer, then upgrade in place with this tree's installer
    and images, adopting the run's derivatives directory as an operator would. The upgrade
    draws the shared level and moves every volume recorded before it to that level."""
    machine, record = rehearsal.machine, rehearsal.record
    stopped = run_installer(machine, record, "stop", "--timeout", "300", timeout=600,
                            installer=record.previous_installer)
    if stopped.returncode != 0:
        _show_output(machine, "the previous aegis-podman stop", stopped)
        raise CheckFailed(f"stop through the previous release: {_last_line(stopped)}")
    # The previous release created the plain derivatives volume but never wrote it. Quadlet
    # keeps an existing volume, so it goes, by its exact name, as the installer asks; the
    # upgrade's units create it again as the bind of the derivatives directory.
    volume = f"{record.prefix}derivatives"
    if volume not in record.volumes:
        raise HarnessError(f"{volume} is not one of the run's volumes")
    exists = _exists(machine, "volume", volume)
    if exists is None:
        raise CheckFailed(f"podman could not tell whether the volume {volume} exists")
    if exists:
        removed = _run(machine, [*PODMAN, "volume", "rm", volume], timeout=120)
        if removed.returncode != 0:
            raise CheckFailed(f"podman volume rm {volume} failed: {_last_line(removed)}")
    images = ",".join(f"{role}={record.images[role]['id']}" for role in ROLES)
    _say(machine, "aegis-podman upgrade")
    result = run_installer(machine, record, "upgrade", "--release", record.release,
                           "--local-images", images, "--derivatives-dir", str(record.derivatives),
                           "--timeout", "600", timeout=UPGRADE_TIMEOUT + 900)
    if result.returncode != 0:
        _show_output(machine, "aegis-podman upgrade", result)
        raise CheckFailed(f"upgrade: {_last_line(result)}")
    state = _installer_state(rehearsal)
    level, migration = state.get("label_level"), state.get("level_migration")
    derivatives = state.get("derivatives")
    problems = []
    if state.get("release") != record.release or state.get("pending") is not None:
        problems.append(f"the record is not on {record.release} with nothing pending")
    if not isinstance(level, str) or LABEL_LEVEL.fullmatch(level) is None:
        problems.append(f"the record names no shared level ({level!r})")
    postgres = f"{record.prefix}postgres-data"
    if not isinstance(migration, dict) or postgres not in migration:
        problems.append(f"the record names no level migration of {postgres}")
    elif not all(done is True for done in migration.values()):
        problems.append("volumes left at their old levels: " + ", ".join(
            sorted(name for name, done in migration.items() if done is not True)))
    if not isinstance(derivatives, dict) or derivatives.get("path") != str(record.derivatives) \
            or derivatives.get("prepared") is not True:
        problems.append(f"the record does not name {record.derivatives} as prepared")
    if problems:
        raise CheckFailed("; ".join(problems))
    rehearsal.level = str(level)
    assert isinstance(migration, dict)
    return (f"stopped, then upgraded to {record.release} with this tree's installer: shared "
            f"level {level}, {len(migration)} recorded volumes relabelled, the derivatives "
            "directory adopted")


def rehearse_start(rehearsal: Rehearsal) -> str:
    """Start on the shared level: PostgreSQL must read the data it wrote at its old level."""
    machine, record = rehearsal.machine, rehearsal.record
    _say(machine, "aegis-podman start")
    result = run_installer(machine, record, "start", "--timeout", str(START_TIMEOUT),
                           timeout=START_TIMEOUT + 300)
    if result.returncode != 0:
        _show_output(machine, "aegis-podman start", result)
        raise CheckFailed(f"start: {_last_line(result)}")
    name = rehearsal.layout.containers[f"{record.prefix}postgres.service"]
    entry = inspect_container(machine, name)
    health, level = health_status(entry), _mcs_level(entry or {})
    if health != "healthy" or level != rehearsal.level:
        raise CheckFailed(f"{name} is {health} at the level {level}, not healthy at "
                          f"{rehearsal.level}")
    return f"every unit active and healthy; {name} healthy at the shared level {level}"


def rehearse_marker_survives(rehearsal: Rehearsal) -> str:
    """The marker written before the move is still there: bootstrap-password through this
    tree's installer finds the same administrator and creates none."""
    user_id, created = _bootstrap(rehearsal, INSTALLER)
    if created or user_id != rehearsal.marker:
        raise CheckFailed(f"bootstrap-password reported user_id={user_id} "
                          f"created={str(created).lower()}, not the marker administrator "
                          f"{rehearsal.marker} found again")
    return f"administrator {user_id} from before the upgrade found again (created=false)"


REHEARSAL_STEPS: dict[str, Callable[[Rehearsal], str]] = {
    "level_migration_previous_install": rehearse_previous_install,
    "level_migration_marker": rehearse_marker,
    "level_migration_upgrade": rehearse_upgrade,
    "level_migration_start": rehearse_start,
    "level_migration_marker_survives": rehearse_marker_survives,
}


def rehearse_level_migration(rehearsal: Rehearsal, report: Report) -> None:
    """Each step in order; after a failed one, the rest are reported as not run."""
    for index, name in enumerate(REHEARSAL_CHECKS):
        try:
            detail = REHEARSAL_STEPS[name](rehearsal)
        except (CheckFailed, HarnessError) as error:
            report.check(name, False, str(error))
        except (OSError, ValueError, KeyError, TypeError) as error:
            report.check(name, False, f"{type(error).__name__}: {error}")
        else:
            report.check(name, True, detail)
            continue
        for later in REHEARSAL_CHECKS[index + 1:]:
            report.check(later, False, f"not run: {name} failed")
        return


# -- cleanup -----------------------------------------------------------------------------------


def _report_additions(
    machine: Machine, before: Mapping[str, Any], after: Mapping[str, Any],
) -> None:
    """Name what is new since the run began, such as build cache: never removed, only
    reported, so the operator sees it."""
    added = {kind: sorted(set(after.get(kind, {})) - set(before.get(kind, {})))
             for kind in inventory.KINDS}
    parts = []
    if added["images"]:
        parts.append(f"{len(added['images'])} new images (build cache)")
    for kind in ("containers", "volumes", "networks"):
        if added[kind]:
            parts.append(f"new {kind}: {', '.join(added[kind])}")
    if parts:
        _say(machine, f"kept, as any build's: {'; '.join(parts)}")


def remove_images(machine: Machine, record: Record) -> list[str]:
    """Untag exactly the run's tags, then remove the images the run built, by ID.

    An image is touched only when it carries the run's label. An image whose ID existed
    before the run is only untagged. A removal by ID refuses an image another name or a
    container still uses, and that is reported, not forced; it never prunes the image's
    untagged parents, which may predate the run.
    """
    problems = []
    for image in run_images(record):
        tag, identifier = image["tag"], image["id"]
        if identifier is None:  # interrupted mid-build: the tag, if any, names the image
            entry = inspect_image(machine, tag) if _exists(machine, "image", tag) else None
            identifier = entry.get("Id") if entry is not None else None
            if entry is None or not re.fullmatch("[0-9a-f]{64}", str(identifier)):
                continue
        elif not _exists(machine, "image", identifier):
            continue
        else:
            entry = inspect_image(machine, identifier)
        if entry is None or _labels(entry).get(RUN_LABEL) != record.run:
            problems.append(f"{tag} does not carry this run's label; left in place")
            continue
        names = entry.get("RepoTags") or entry.get("Names") or []
        if tag in names:
            untagged = _run(machine, [*PODMAN, "untag", identifier, tag], timeout=120)
            if untagged.returncode != 0:
                problems.append(f"podman untag {tag} failed: {_last_line(untagged)}")
                continue
        if image["preexisting"]:
            continue
        # --no-prune: an untagged parent, such as build cache from an earlier build, stays.
        removed = _run(machine, [*PODMAN, "image", "rm", "--no-prune", identifier],
                       timeout=300)
        if removed.returncode != 0:
            problems.append(f"podman image rm {identifier} failed: {_last_line(removed)}")
    return problems


def remove_run_directory(machine: Machine, record: Record) -> list[str]:
    """The run's own directory, once the installer's record in it is gone. Files the
    containers' users own there go through ``podman unshare``, and only there."""
    path = Path(record.run_directory)
    if not os.path.lexists(path):
        return []
    problem = directory_problem(machine, record)
    if problem is not None:
        return [problem]
    if os.path.lexists(record.state_home / "state.json"):
        return [f"kept {path}: the installer's record is still in it"]
    with contextlib.suppress(OSError):
        shutil.rmtree(path)
    if os.path.lexists(path):
        result = _run(machine, [*PODMAN, "unshare", "rm", "-rf", "--one-file-system", "--",
                                str(path)], timeout=300)
        if os.path.lexists(path):
            return [f"could not remove {path}: {_last_line(result)}"]
    return []


def directory_problem(machine: Machine, record: Record) -> str | None:
    """Why the run directory, if it exists, is not the one this run created, or None."""
    path = Path(record.run_directory)
    if path != state_directory(machine) / record.run:
        return f"{path} is not the run's own path; left in place"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    identity = record.directory_identity or {}
    try:
        token = _read_private(path / DIRECTORY_TOKEN, machine.uid).strip()
    except (OSError, HarnessError, UnicodeDecodeError):
        token = None
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != machine.uid
        or info.st_ino != identity.get("inode") or token != identity.get("token")
    ):
        return f"{path} is not the directory this run created; left in place"
    return None


def _read_private(path: Path, uid: int) -> str:
    """A regular file of the operator's, never through a link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            raise HarnessError(f"{path} is not a regular file of yours")
        return handle.read(1 << 20).decode("utf-8")


def identity_problems(machine: Machine, record: Record) -> list[str]:
    """Before --cleanup changes anything: every recorded identity that still exists must
    still be the run's. Something already gone is fine; a mismatch refuses the cleanup.

    The installer's own purge checks what it recorded (unit files by hash, volumes and
    networks by their install label), and refuses as a whole on any mismatch.
    """
    problems = []
    problem = directory_problem(machine, record)
    if problem is not None:
        problems.append(problem)
    state = record.state_home / "state.json"
    if problem is None and os.path.lexists(state):
        try:
            data = json.loads(_read_private(state, machine.uid))
        except (OSError, HarnessError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict) or data.get("prefix") != record.prefix:
            problems.append(f"{state} is not the installer's record for {record.prefix}")
    for image in run_images(record):
        tag, identifier = image["tag"], image["id"]
        exists = _exists(machine, "image", tag)
        if exists is None:
            problems.append(f"podman could not tell whether {tag} exists")
        elif exists:
            entry = inspect_image(machine, tag) or {}
            if _labels(entry).get(RUN_LABEL) != record.run:
                problems.append(f"{tag} names an image without this run's label")
            elif identifier is not None and entry.get("Id") != identifier:
                problems.append(f"{tag} now names another image than the run built")
        if (
            identifier is not None and _exists(machine, "image", identifier)
            and _labels(inspect_image(machine, identifier) or {}).get(RUN_LABEL) != record.run
        ):
            problems.append(f"image {identifier} does not carry this run's label")
    for name in record.containers:
        exists = _exists(machine, "container", name)
        if exists is None:
            problems.append(f"podman could not tell whether the container {name} exists")
        elif exists:
            labels = ((inspect_container(machine, name) or {}).get("Config") or {}).get(
                "Labels") or {}
            if labels.get("PODMAN_SYSTEMD_UNIT") != f"{name}.service":
                problems.append(f"the container {name} does not belong to the unit "
                                f"{name}.service")
    return problems


def leftovers(machine: Machine, record: Record) -> list[str]:
    """What of the run still exists, looked up by its exact recorded identities. Unit files
    and drop-in directories are named without their directory, which the record fixes."""
    found = [f"file {os.path.basename(path)}"
             for path in (*record.unit_files, *record.drop_in_directories)
             if os.path.lexists(path)]
    try:
        states = unit_states(machine, record.units)
    except CheckFailed as error:
        found.append(str(error))
    else:
        found += [f"unit {unit} is {info['LoadState']}/{info['ActiveState']}"
                  for unit, info in states.items()
                  if info["LoadState"] != "not-found" or info["ActiveState"] != "inactive"]
    for kind, names in (("container", record.containers), ("volume", record.volumes),
                        ("network", record.networks)):
        for name in names:
            exists = _exists(machine, kind, name)
            if exists is not False:
                found.append(f"{kind} {name}" + ("" if exists else " (podman could not tell)"))
    for image in run_images(record):
        references = [image["tag"]]
        if image["id"] is not None and not image["preexisting"]:
            references.append(image["id"])
        found += [f"image {reference}" for reference in references
                  if _exists(machine, "image", reference) is not False]
    if os.path.lexists(record.run_directory):
        found.append(record.run_directory)
    if machine.port_in_use(record.port):
        found.append(f"port {record.port} still has a listener")
    return found


def cleanup(machine: Machine, record: Record, record_path: Path, report: Report) -> bool:
    """Stop and purge the stack through the installer, remove the run's images and
    directory by exact identity, and report anything of the run that is left."""
    problems: list[str] = []
    if os.path.lexists(record.state_home / "state.json"):
        _say(machine, "aegis-podman stop, then uninstall --purge-data")
        run_installer(machine, record, "stop", "--timeout", "300", timeout=600)
        result = run_installer(machine, record, "uninstall", "--purge-data", "--timeout", "300",
                               timeout=900)
        if result.returncode != 0:
            _show_output(machine, "aegis-podman uninstall --purge-data", result)
            problems.append(f"aegis-podman uninstall --purge-data: {_last_line(result)}")
    # Units that failed stay in systemd's memory until this, by their exact names.
    _run(machine, [*SYSTEMCTL, "reset-failed", *record.units], timeout=60)
    for directory in record.created_directories:  # deepest first; only an empty one goes
        with contextlib.suppress(OSError):
            os.rmdir(directory)
    problems += remove_images(machine, record)
    problems += remove_run_directory(machine, record)
    left = leftovers(machine, record)
    for item in [*problems, *(f"left: {item}" for item in left)]:
        _say(machine, f"cleanup: {item}")
    clean = not problems and not left
    detail = "; ".join([*problems, *(f"left: {item}" for item in left)])
    report.check("cleanup", clean, detail or "nothing of the run is left")
    changes: list[str] = []
    if record.inventory:
        try:
            after = inventory.snapshot(
                lambda arguments: machine.run([*PODMAN, *arguments], timeout=120),
            )
            changes = inventory.compare(record.inventory, after)
        except Exception as error:
            changes = [f"cannot take the Podman inventory: {type(error).__name__}: {error}"]
            after = None
        report.check("preexisting_unchanged", not changes, "; ".join(changes)
                     or "no Podman object that existed before the run was removed or changed")
        if after is not None:
            _report_additions(machine, record.inventory, after)
    if clean:
        # Nothing of the run is left, so its record has done its job. A changed pre-existing
        # object is reported above; holding the record would not undo it, and would only
        # block the next run.
        record_path.unlink(missing_ok=True)
        if changes:
            _say(machine, "the run left nothing behind, so its record is removed; check the "
                          "pre-existing objects reported above")
        return not changes
    else:
        _say(machine, f"the run record stays at {record_path}; after fixing the cause, finish "
                      "the cleanup with: uv run --locked python scripts/podman_deploy_check.py "
                      f"--cleanup {record_path}")
    return clean


# -- signals and the run -----------------------------------------------------------------------


def _raise_interrupted(signum: int, frame: FrameType | None) -> None:
    raise Interrupted(signum)


SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _ignore_signals() -> None:
    """From here on the signals are ignored, and the programs cleanup runs inherit that."""
    for signum in SIGNALS:
        signal.signal(signum, signal.SIG_IGN)


@contextlib.contextmanager
def interruptible() -> Iterator[Callable[[], None]]:
    """Ctrl-C, SIGTERM and SIGHUP end the run. The caller switches straight to ignoring
    them, with the function this yields, before its cleanup: there is no moment in between
    in which a signal would kill the harness. The original handlers return only at the
    end."""
    previous = {signum: signal.getsignal(signum) for signum in SIGNALS}
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _raise_interrupted)
    signal.signal(signal.SIGHUP, _raise_interrupted)
    try:
        yield _ignore_signals
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextlib.contextmanager
def uninterruptible() -> Iterator[None]:
    """Cleanup is not cut short: the signals are ignored, and the programs it runs inherit
    that. An interrupted cleanup leaves the record for --cleanup."""
    previous = {signum: signal.signal(signum, signal.SIG_IGN) for signum in SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _summary(report: Report, interrupted: int | None) -> None:
    failed = [name for name, passed in report.results.items() if not passed]
    result = "interrupted" if interrupted is not None else (
        "passed" if report.passed else "failed")
    emit(report.machine, f"AEGIS_PODMAN_DEPLOY result={result} "
                         f"passed={len(report.results) - len(failed)} failed={len(failed)}")


def _exercise(
    machine: Machine, record: Record, record_path: Path, layout: Layout, address: str,
    report: Report, rehearse: str | None = None,
) -> None:
    run_directory = Path(record.run_directory)
    run_directory.mkdir(mode=0o700)
    run_directory.chmod(0o700)
    token = secrets.token_hex(16)
    descriptor = os.open(run_directory / DIRECTORY_TOKEN,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, f"{token}\n".encode("ascii"))
    finally:
        os.close(descriptor)
    record.directory_identity = {"inode": run_directory.lstat().st_ino, "token": token}
    save_record(record_path, record)
    record.temporary.mkdir(mode=0o700)
    # Empty and the operator's, as the installer requires: it prepares it for the containers.
    record.derivatives.mkdir(mode=0o750)
    record.derivatives.chmod(0o750)
    if rehearse is not None:
        if not build_images(machine, record, record_path, report):
            for name in REHEARSAL_CHECKS:
                report.check(name, False, "not run: the images were not built")
            return
        record.installer_started = True
        save_record(record_path, record)
        rehearse_level_migration(
            Rehearsal(machine, record, record_path, layout, address, rehearse), report,
        )
        return
    ready = build_images(machine, record, record_path, report) and install_stack(
        machine, record, record_path, address, report,
    )
    if not ready:
        for name in CHECKS:
            report.check(name, False, "not run: the stack did not come up")
        return
    run_checks(Context(machine, record, layout, address), report)


def verify(machine: Machine, rehearse: str | None = None) -> int:
    if machine.uid == 0:
        raise HarnessError("refusing to run as root")
    records = record_directory(machine)
    with locked(records):
        stale = sorted(path for path in records.iterdir() if path.name.endswith(".json"))
        if stale:
            raise HarnessError(
                f"an earlier run left {stale[0]}; finish its cleanup first with: uv run "
                f"--locked python scripts/podman_deploy_check.py --cleanup {stale[0]}"
            )
        run = f"t{machine.token(4)}"
        if RUN_ID.fullmatch(run) is None:
            raise HarnessError(f"malformed run ID {run!r}")
        prefix = checked_prefix(f"aegis-{run}-")
        layout = layout_for(prefix, config_home(machine))
        open_log(machine, records / f"{run}.log")
        try:
            return _verify_run(machine, records, run, prefix, layout, rehearse)
        finally:
            close_log(machine)


def _verify_run(
    machine: Machine, records: Path, run: str, prefix: str, layout: Layout,
    rehearse: str | None = None,
) -> int:
    report = Report(machine)
    run_directory = records / run
    problems, facts = preflight(machine, layout, run_directory)
    if problems:
        report.check("preflight", False, "; ".join(problems))
        _summary(report, None)
        return 1
    report.check("preflight", True, f"run {run}, prefix {prefix}, test port {facts.port}")
    record = new_record(machine, run, layout, facts.port)
    record.inventory = facts.inventory
    record.created_directories = [
        path for path in unit_directory_chain(config_home(machine))
        if not os.path.lexists(path)
    ]
    record_path = records / f"{run}.json"
    interrupted: int | None = None
    failure: BaseException | None = None
    with interruptible() as ignore_signals:
        # Nothing but assignments before the cleanup: no print may come first, and
        # output is best effort anyway.
        try:
            save_record(record_path, record)
            _exercise(machine, record, record_path, layout, facts.address, report, rehearse)
        except KeyboardInterrupt as error:
            interrupted = error.signum if isinstance(error, Interrupted) else signal.SIGINT
        except Exception as error:
            failure = error
        finally:
            ignore_signals()
            cleaned = None
            if record_path.exists():
                cleaned = _guarded_cleanup(machine, record, record_path, report)
        if failure is not None:
            report.check("harness", False, f"{type(failure).__name__}: {failure}")
        if interrupted is not None:
            outcome = {None: "nothing had been created", True: "the cleanup finished",
                       False: "the cleanup left the record for --cleanup"}[cleaned]
            _say(machine, f"interrupted by signal {interrupted}; {outcome}")
        _summary(report, interrupted)
    if interrupted is not None:
        return 128 + int(interrupted)
    return 0 if report.passed else 1


def _guarded_cleanup(
    machine: Machine, record: Record, record_path: Path, report: Report,
) -> bool:
    """Cleanup that reports, rather than raises, a failure of its own; the record stays."""
    try:
        return cleanup(machine, record, record_path, report)
    except Exception as error:
        report.check("cleanup", False, f"{type(error).__name__}: {error}")
        _say(machine, f"the run record stays at {record_path}; finish the cleanup with: uv run "
                      f"--locked python scripts/podman_deploy_check.py --cleanup {record_path}")
        return False


def finish_cleanup(machine: Machine, path: Path) -> int:
    """--cleanup: finish an interrupted run's cleanup by the identities its record names."""
    records = record_directory(machine)
    with locked(records):
        record = load_record(machine, path, records)
        open_log(machine, records / f"{record.run}.log")
        try:
            report = Report(machine)
            problems = identity_problems(machine, record)
            if problems:
                report.check("cleanup", False, "refused, nothing was changed: "
                             + "; ".join(problems))
                _say(machine, f"the record stays at {path}; check what changed before "
                              "cleaning up")
                _summary(report, None)
                return 1
            with uninterruptible():
                clean = _guarded_cleanup(machine, record, path, report)
            _summary(report, None)
            return 0 if clean else 1
        finally:
            close_log(machine)


def main(argv: Sequence[str] | None = None, machine: Machine | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="podman_deploy_check", allow_abbrev=False,
        description="Install, check and remove a run-unique rootless Podman Quadlet stack.",
    )
    parser.add_argument("--cleanup", metavar="RECORD", type=Path,
                        help="finish an interrupted run's cleanup from its record")
    parser.add_argument(
        "--rehearse-level-migration", action="store_true",
        help="instead of the checks: install the release at --from-commit with its own "
             "installer and images, write a marker, upgrade it in place with this tree's, and "
             "check that the one-time move to the shared SELinux level keeps PostgreSQL's data",
    )
    parser.add_argument(
        "--from-commit", metavar="COMMIT",
        help=f"the rehearsal's previous release, a commit ID whose installer is from before "
             f"the shared level (default {PRE_LEVEL_COMMIT})",
    )
    args = parser.parse_args(argv)
    if args.cleanup is not None and args.rehearse_level_migration:
        parser.error("--cleanup goes alone")
    if args.from_commit is not None and not args.rehearse_level_migration:
        parser.error("--from-commit goes with --rehearse-level-migration")
    if args.from_commit is not None and COMMIT.fullmatch(args.from_commit) is None:
        parser.error("--from-commit must be a commit ID: 7 to 40 lowercase hex digits")
    rehearse = (args.from_commit or PRE_LEVEL_COMMIT) if args.rehearse_level_migration else None
    machine = Machine() if machine is None else machine
    try:
        if args.cleanup is not None:
            return finish_cleanup(machine, args.cleanup)
        return verify(machine, rehearse)
    except HarnessError as error:
        emit(machine, f"podman_deploy_check: {error}", error=True)
        return 1
    except KeyboardInterrupt:
        # Before a run records anything, or right after its cleanup; a record left behind
        # names what --cleanup still has to do.
        emit(machine, "podman_deploy_check: interrupted", error=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
