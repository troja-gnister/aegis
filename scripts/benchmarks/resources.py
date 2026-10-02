"""Owned, exactly identified resources for one benchmark run.

Every run creates a fresh private workspace, a uniquely named Compose project with
its own SSD-backed (engine volume) database storage, generated secrets, and
loopback-only ephemeral ports. Cleanup removes only the recorded resource identities
and recorded workspace entries; anything unknown or replaced is preserved and
reported. Reports live outside the workspace and survive teardown. There is no way
to point the benchmark at an existing database, project or operator volume.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    sanitized_environment,
)
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResourceError,
    ProjectResourceRule,
    ResourceRunner,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
)
from aegisctl.mounts import local_identity

from .dataset import ROOTS

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE_PATH = Path(__file__).with_name("phase2a-profile.json")
PROJECT_PREFIX = "aegis-bench-"
PROJECT_RE = re.compile(r"aegis-bench-[0-9a-f]{12}\Z", re.ASCII)
TOKEN_RE = re.compile(r"[0-9a-f]{12}\Z", re.ASCII)
# Operator data: never recordable, never a cleanup target.
OPERATOR_VOLUMES = frozenset({"aegis_postgres-data"})
WORK_PREFIX = "aegis-bench."
SECRET_NAMES = (
    "postgres-superuser-password", "db-migrator-password", "db-web-password",
    "db-operations-password", "db-indexer-password", "db-media-password",
    "django-secret-key", "auth-throttle-hmac-key", "e2e-alice-password",
    "e2e-bob-password", "e2e-admin-password", "bench-user-password",
)
SERVICES = ("postgres", "migrate", "web", "operations", "indexer", "media", "gateway")
NETWORKS = ("backend", "edge", "tls-hop")
VOLUMES = (
    "indexer-coordination", "postgres-data", "staging", "derivatives",
    "model-cache", "quarantine", "frontier-outbox",
)
CPUSET = "0-3"
ONEOFF_SERVICES = frozenset({"migrate", "web"})
SCRIPT_MOUNTS = {
    str(REPOSITORY / "scripts/__init__.py"): "/app/scripts/__init__.py",
    str(REPOSITORY / "scripts/benchmarks"): "/app/scripts/benchmarks",
}
RELEASE_ID = "phase2a-benchmark"
STORAGE_BUDGET_BYTES = 8 * 1024**3
STORAGE_MARGIN_BYTES = 4 * 1024**3
PROFILE_KEYS = frozenset({
    "version", "package", "seed", "entries", "wideFolderChildren", "users",
    "warmupSeconds", "measurementSeconds", "pageSize", "mix", "wideFolderListShareMinimum",
    "coldRestartRecordedSeparately", "workerLoad", "referenceHost", "memoryLimitsMiB",
    "storageCalibration", "referenceCertification",
})
MIX_KEYS = frozenset({"directoryPages", "alternateSorts", "details", "basicFilters",
                      "indexStatus"})
LIMIT_KEYS = frozenset({"postgres", "web", "operations", "indexer", "media", "gateway"})


class BenchmarkResourceError(RuntimeError):
    """A benchmark input or resource could not be proved fresh, owned and safe."""


def _positive(value: object, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    """Load the versioned workload profile, refusing any drift in its schema."""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkResourceError("benchmark profile is unreadable") from exc
    invalid = BenchmarkResourceError("benchmark profile is invalid")
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise invalid
    mix = profile["mix"]
    limits = profile["memoryLimitsMiB"]
    if (
        profile["version"] != 1 or profile["package"] != "2A.1"
        or not _positive(profile["entries"], 20_000_000)
        or not _positive(profile["wideFolderChildren"], 1_000_000)
        or not _positive(profile["users"], 64) or type(profile["seed"]) is not int
        or not _positive(profile["warmupSeconds"], 86_400)
        or not _positive(profile["measurementSeconds"], 86_400)
        or not _positive(profile["pageSize"], 250)
        or not isinstance(mix, dict) or set(mix) != MIX_KEYS
        or any(not _positive(value, 100) for value in mix.values()) or sum(mix.values()) != 100
        or type(profile["wideFolderListShareMinimum"]) is not float
        or not 0.25 <= profile["wideFolderListShareMinimum"] <= 1
        or profile["coldRestartRecordedSeparately"] is not True
        or not isinstance(limits, dict) or set(limits) != LIMIT_KEYS
        or any(not _positive(value, 65_536) for value in limits.values())
        or not isinstance(profile["referenceHost"], dict)
        or not isinstance(profile["storageCalibration"], dict)
        or profile["referenceCertification"] != "requires_calibrated_reference_host"
        or profile["workerLoad"] != "none_for_catalog_seed_mode"
    ):
        raise invalid
    return profile


def _within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def validate_report_dir(value: str | Path, *, forbidden: Iterable[Path] = ()) -> Path:
    """Accept only a fresh, empty, private, owned, non-root directory outside sources."""
    refused = BenchmarkResourceError("report directory must be a fresh private owned directory")
    path = Path(value)
    try:
        if not path.is_absolute() or len(path.parts) < 3:
            raise refused
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_uid == 0 or metadata.st_mode & 0o077
            or path.resolve(strict=True) != path
        ):
            raise refused
        if any(path.iterdir()):
            raise refused
    except OSError as exc:
        raise refused from exc
    for parent in (REPOSITORY, *forbidden):
        if _within(path, parent.resolve()):
            raise refused
    return path


def project_name(token: str) -> str:
    name = f"{PROJECT_PREFIX}{token}"
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise BenchmarkResourceError("benchmark project token is invalid")
    if PROJECT_RE.fullmatch(name) is None:
        raise BenchmarkResourceError("benchmark project name is invalid")
    return name


def _require_project(project: str) -> None:
    if not isinstance(project, str) or PROJECT_RE.fullmatch(project) is None:
        raise BenchmarkResourceError("benchmark project name is invalid")


def benchmark_environment_variables(
    inherited: Mapping[str, str], *, work: Path, project: str, http_port: int, db_port: int,
    database: str,
) -> dict[str, str]:
    """Inherit only tool routing; every application/database input is generated."""
    _require_project(project)
    env = sanitized_environment(inherited)
    for key in list(env):
        if key in ("DATABASE_URL", "PYTHONPATH") or key.endswith("_DATABASE_URL"):
            del env[key]
    return env | {
        "AEGIS_ENV": "test", "AEGIS_RELEASE_ID": RELEASE_ID,
        "AEGIS_PUBLIC_URL": f"http://127.0.0.1:{http_port}",
        "AEGIS_ALLOWED_HOSTS": "127.0.0.1,localhost,web",
        "AEGIS_HTTP_PORT": f"127.0.0.1:{http_port}", "AEGIS_TEST_DB_PORT": str(db_port),
        "AEGIS_TEST_SECRET_DIR": str(work / "secrets"),
        "AEGIS_TEST_RESOURCE_TOKEN": work.name,
        "AEGIS_UID": str(os.getuid()), "AEGIS_GID": str(os.getgid()),
        "AEGIS_DB_NAME": database, "AEGIS_DB_HOST": "postgres", "AEGIS_DB_PORT": "5432",
        "DJANGO_SETTINGS_MODULE": "aegis.settings.test",
    }


def _bytes(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise BenchmarkResourceError("benchmark Compose memory limit is invalid")


def check_compose_config(config: Mapping[str, Any], *, project: str,
                         profile: Mapping[str, Any],
                         cpu: tuple[str, object] = ("cpuset", CPUSET)) -> None:
    """Refuse operator volumes, public ports, writable originals or missing limits."""
    _require_project(project)
    if config.get("name") != project:
        raise BenchmarkResourceError("benchmark Compose project changed")
    for volume in config.get("volumes", {}).values():
        name = volume.get("name", "")
        if (
            volume.get("external") or name in OPERATOR_VOLUMES
            or not name.startswith(f"{project}_")
        ):
            raise BenchmarkResourceError("benchmark volumes must be fresh and project-scoped")
    if not config.get("networks", {}).get("backend", {}).get("internal"):
        raise BenchmarkResourceError("benchmark backend network must be internal")
    services = config.get("services", {})
    if set(SERVICES) - set(services):
        raise BenchmarkResourceError("benchmark Compose services are incomplete")
    limits = profile["memoryLimitsMiB"]
    for role, service in services.items():
        if role not in SERVICES:
            raise BenchmarkResourceError("benchmark Compose has an unexpected service")
        if role in limits:
            expected = limits[role] * 1024 * 1024
            if "mem_limit" not in service or _bytes(service["mem_limit"]) != expected:
                raise BenchmarkResourceError("benchmark memory limits are missing")
            value = service.get(cpu[0])
            matches = (str(value) == str(cpu[1]) if cpu[0] == "cpuset" else
                       isinstance(value, (int, float, str)) and float(value) == float(str(cpu[1])))
            if not matches:
                raise BenchmarkResourceError("benchmark CPU placement is missing")
        if role != "postgres" and (not service.get("read_only")
                                   or service.get("cap_drop") != ["ALL"]):
            raise BenchmarkResourceError("benchmark runtime isolation is missing")
        if role not in ("gateway", "postgres") and set(service.get("networks", {})) != {
            "backend",
        }:
            raise BenchmarkResourceError("benchmark backend must have no internet egress")
        for port in service.get("ports", []) or []:
            if role not in ("gateway", "postgres") or port.get("host_ip") != "127.0.0.1":
                raise BenchmarkResourceError("benchmark ports must bind loopback only")
        for volume in service.get("volumes", []) or []:
            target = str(volume.get("target", ""))
            source = Path(str(volume.get("source"))).resolve()
            if volume.get("type") == "bind" and (
                source in REPOSITORY.parents or _within(source, REPOSITORY)
            ):
                raise BenchmarkResourceError("benchmark runtime cannot mount the repository")
            if target.startswith("/srv/aegis/roots/"):
                if role in ("web", "migrate", "postgres"):
                    raise BenchmarkResourceError("web/migrator cannot mount originals")
                if volume.get("read_only") is not True:
                    raise BenchmarkResourceError("every original mount must be read-only")


def _fields(fingerprint: str) -> dict[str, Any]:
    try:
        value = json.loads(fingerprint)
    except ValueError as exc:
        raise BenchmarkResourceError("benchmark resource fingerprint is invalid") from exc
    if not isinstance(value, dict):
        raise BenchmarkResourceError("benchmark resource fingerprint is invalid")
    return value


def _require_owned_inventory(inventory: ProjectInventory) -> None:
    _require_project(inventory.project)
    for resource in inventory.resources:
        fields = _fields(resource.fingerprint)
        name = str(fields.get("Name", "")).lstrip("/")
        if resource.handle in OPERATOR_VOLUMES or name in OPERATOR_VOLUMES:
            raise BenchmarkResourceError("operator volume is never a benchmark resource")
        if not name.startswith(inventory.project):
            raise BenchmarkResourceError("benchmark resource is not project-named")


def record_resources(
    project: str, environment: Mapping[str, str], *, runner: ResourceRunner | None = None,
    expected: ProjectInventory | None = None,
    allowed_new: tuple[ProjectResourceRule, ...] = (),
) -> ProjectInventory:
    """Capture (and, given a prior inventory, admit) exact project resource identities."""
    _require_project(project)
    observed = capture_project_inventory(project, environment, runner=runner)
    if expected is not None:
        observed = admit_project_transition(expected, observed, allowed_new)
    _require_owned_inventory(observed)
    return observed


def cleanup_resources(inventory: ProjectInventory, environment: Mapping[str, str], *,
                      runner: ResourceRunner | None = None) -> None:
    """Remove exactly the recorded identities; refuse unknown or replaced resources."""
    _require_owned_inventory(inventory)
    cleanup_project_inventory(inventory, environment, runner=runner)


def resource_rules(token_label: str) -> tuple[ProjectResourceRule, ...]:
    provenance = ("aegis.test.resource-token", token_label)
    return (
        *(ProjectResourceRule("container", (
            ("com.docker.compose.service", service), ("com.docker.compose.oneoff", "False"),
            provenance,
        )) for service in SERVICES),
        *(ProjectResourceRule("network", (
            ("com.docker.compose.network", network), provenance,
        )) for network in NETWORKS),
        *(ProjectResourceRule("volume", (
            ("com.docker.compose.volume", volume), provenance,
        )) for volume in VOLUMES),
    )


Identity = tuple[int, ...]


def _identity(path: Path) -> Identity:
    metadata = path.lstat()
    kind = stat.S_IFMT(metadata.st_mode)
    base = (metadata.st_dev, metadata.st_ino, kind, metadata.st_uid, metadata.st_gid)
    if kind == stat.S_IFREG:
        return (*base, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns)
    return base


@dataclass
class Workspace:
    """A private /tmp workspace whose every entry is recorded at creation."""

    path: Path
    entries: dict[str, Identity] = field(default_factory=dict)
    ancestor: Identity = ()
    secret_values: list[str] = field(default_factory=list)

    @classmethod
    def create(cls) -> Workspace:
        path = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir="/tmp")).resolve()
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077 or any(path.iterdir())
        ):
            raise BenchmarkResourceError("benchmark workspace was not created private and empty")
        parent = path.parent.lstat()
        return cls(path, {".": _identity(path)}, (parent.st_dev, parent.st_ino))

    def _target(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not relative:
            raise BenchmarkResourceError("benchmark workspace path is invalid")
        parent = str(candidate.parent) if str(candidate.parent) != "" else "."
        if parent not in self.entries:
            raise BenchmarkResourceError("benchmark workspace parent was not recorded")
        if relative in self.entries:
            raise BenchmarkResourceError("benchmark workspace entry already exists")
        return self.path / candidate

    def directory(self, relative: str, *, mode: int = 0o700) -> Path:
        target = self._target(relative)
        target.mkdir(mode=0o700)
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
        self.entries[relative] = _identity(target)
        return target

    def file(self, relative: str, content: bytes, *, mode: int = 0o600) -> Path:
        target = self._target(relative)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, mode)
            view = memoryview(content)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.entries[relative] = _identity(target)
        return target

    def secret(self, relative: str, value: str) -> Path:
        self.secret_values.append(value)
        return self.file(relative, (value + "\n").encode("ascii"))

    def adopt(self, relative: str) -> Path:
        """Record a regular file a trusted, awaited tool just created here."""
        target = self._target(relative)
        identity = _identity(target)
        if identity[2] != stat.S_IFREG or identity[3] != os.geteuid() or identity[5] != 1:
            raise BenchmarkResourceError("benchmark workspace output is unsafe")
        self.entries[relative] = identity
        return target

    def validate(self) -> None:
        parent = self.path.parent.lstat()
        if (parent.st_dev, parent.st_ino) != self.ancestor:
            raise BenchmarkResourceError("benchmark workspace ancestor changed")
        current: dict[str, Identity] = {}
        pending = [self.path]
        while pending:
            target = pending.pop()
            relative = "." if target == self.path else target.relative_to(self.path).as_posix()
            try:
                current[relative] = _identity(target)
            except OSError as exc:
                raise BenchmarkResourceError("benchmark workspace entry changed") from exc
            if stat.S_ISDIR(target.lstat().st_mode):
                pending.extend(Path(child.path) for child in os.scandir(target))
        if set(current) - set(self.entries):
            raise BenchmarkResourceError("benchmark workspace contains unknown entries")
        if any(current.get(key) != value for key, value in self.entries.items()):
            raise BenchmarkResourceError("benchmark workspace entry changed")

    def cleanup(self) -> None:
        self.validate()
        for relative in sorted(self.entries, key=lambda key: -len(Path(key).parts)):
            if relative == ".":
                continue
            target = self.path / relative
            if stat.S_ISDIR(target.lstat().st_mode):
                target.rmdir()
            else:
                target.unlink()
        self.path.rmdir()


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run(command: list[str], env: Mapping[str, str], *, timeout: int,
         stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=REPOSITORY, env=dict(env), input=stdin, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


@dataclass
class BenchmarkEnvironment:
    """Handles to one owned stack; no method accepts an external target."""

    profile: Mapping[str, Any]
    report_dir: Path
    workspace: Workspace
    project: str
    token: str
    database: str
    env: dict[str, str]
    compose_arguments: tuple[str, ...]
    http_port: int
    db_port: int
    inventory: ProjectInventory | None = None
    log: Callable[[str], None] = print
    cpu: tuple[str, object] = ("cpuset", CPUSET)
    storage: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self.workspace.secret_values)

    def secret_path(self, name: str) -> Path:
        if name not in SECRET_NAMES:
            raise BenchmarkResourceError("unknown benchmark secret")
        return self.workspace.path / "secrets" / name

    def password(self) -> str:
        return self.secret_path("bench-user-password").read_text(encoding="ascii").strip()

    def compose(self, *arguments: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        environment = compose_environment(self.env)
        return _run(compose_command(*self.compose_arguments, *arguments, environment=environment),
                    environment, timeout=timeout)

    def engine(self, *arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return _run(container_command(*arguments, environment=self.env), self.env,
                    timeout=timeout)

    def container_ids(self) -> dict[str, str]:
        """Recorded service container identities (exact IDs, never name patterns)."""
        if self.inventory is None:
            raise BenchmarkResourceError("benchmark resources were not recorded")
        result: dict[str, str] = {}
        for resource in self.inventory.resources:
            if resource.kind != "container":
                continue
            labels = _fields(resource.fingerprint).get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if service in SERVICES:
                result[service] = resource.immutable_id
        return result

    def storage_check(self) -> dict[str, Any]:
        """Free space and medium of the database volume's backing filesystem.

        Read from inside the owned PostgreSQL container at its data mount, so it is the
        actual engine storage, not the runner's view. Refuses to continue below the 8 GiB
        catalog budget plus margin; an unreadable rotational flag is recorded as unknown.
        """
        ids = self.container_ids()
        script = (
            'df -Pk /var/lib/postgresql | tail -n 1; '
            'awk \'$5=="/var/lib/postgresql" {for (i=7;i<=NF;i++) if ($i=="-") '
            '{print "SOURCE", $(i+1), $(i+2); exit}}\' /proc/self/mountinfo'
        )
        result = self.engine("exec", ids["postgres"], "sh", "-c", script)
        if result.returncode:
            raise BenchmarkResourceError("benchmark storage capacity is unknown")
        lines = result.stdout.splitlines()
        try:
            fields = lines[0].split()
            total, free = int(fields[1]) * 1024, int(fields[3]) * 1024
        except (IndexError, ValueError) as exc:
            raise BenchmarkResourceError("benchmark storage capacity is unknown") from exc
        filesystem, device = "unknown", "unknown"
        for line in lines[1:]:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "SOURCE":
                filesystem, device = parts[1], parts[2]
        rotational = "unknown"
        name = device.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
            probe = (
                f'for f in /sys/class/block/{name}/queue/rotational '
                f'/sys/class/block/{name}/../queue/rotational; do '
                '[ -r "$f" ] && { cat "$f"; exit 0; }; done; '
                f'for d in /sys/block/dm-*; do [ "$(cat "$d/dm/name" 2>/dev/null)" = "{name}" ] '
                '&& { cat "$d/queue/rotational"; exit 0; }; done; exit 1'
            )
            flag = self.engine("exec", ids["postgres"], "sh", "-c", probe)
            if flag.returncode == 0 and flag.stdout.strip() in ("0", "1"):
                rotational = "no (SSD/NVMe)" if flag.stdout.strip() == "0" else "yes"
        required = STORAGE_BUDGET_BYTES + STORAGE_MARGIN_BYTES
        record = {
            "path": "PostgreSQL data volume (engine-managed)", "filesystem": filesystem,
            "totalBytes": total, "freeBytes": free, "requiredFreeBytes": required,
            "catalogBudgetBytes": STORAGE_BUDGET_BYTES, "marginBytes": STORAGE_MARGIN_BYTES,
            "rotational": rotational, "sufficient": free >= required,
        }
        if free < required:
            raise BenchmarkResourceError("benchmark database storage capacity is insufficient")
        return record

    def require_recorded(self) -> None:
        if self.inventory is None:
            raise BenchmarkResourceError("benchmark resources were not recorded")
        current = record_resources(self.project, self.env)
        if current != self.inventory:
            raise BenchmarkResourceError("benchmark resources changed")

    def run_oneoff(self, service: str, command: Sequence[str], *, mounts: Mapping[str, str],
                   environment: Mapping[str, str] | None = None, stdin: str | None = None,
                   timeout: int = 7200) -> subprocess.CompletedProcess[str]:
        """A test-only one-off container inside the owned project's internal network.

        PostgreSQL is reachable only on the internal backend network (Docker does not
        publish ports of internal-only containers), so database work runs here. Only
        the benchmark scripts and named private inputs are mounted, read-only; the
        one-off resource is admitted, recorded and removed like every other identity.
        """
        if service not in ONEOFF_SERVICES:
            raise BenchmarkResourceError("benchmark one-off service is not allowed")
        self.require_recorded()
        self.workspace.validate()
        volumes: list[str] = []
        for source, target in {**SCRIPT_MOUNTS, **mounts}.items():
            if not target.startswith(("/app/scripts", "/run/benchmark/")):
                raise BenchmarkResourceError("benchmark one-off mount target is not allowed")
            volumes += ["--volume", f"{source}:{target}:ro"]
        variables: list[str] = []
        for key, value in {"PYTHONPATH": "/app", **(environment or {})}.items():
            variables += ["--env", f"{key}={value}"]
        assert self.inventory is not None
        expected = self.inventory
        try:
            result = _run(compose_command(
                *self.compose_arguments, "run", "--rm", "--no-deps", "-T", *volumes,
                *variables, service, *command, environment=compose_environment(self.env),
            ), compose_environment(self.env), timeout=timeout, stdin=stdin)
        finally:
            self.inventory = record_resources(
                self.project, self.env, expected=expected,
                allowed_new=(ProjectResourceRule("container", (
                    ("com.docker.compose.service", service),
                    ("com.docker.compose.oneoff", "True"),
                    ("aegis.test.resource-token", self.workspace.path.name),
                )),),
            )
        return result

    def seed(self) -> dict[str, Any]:
        result = self.run_oneoff("migrate", [
            "python", "manage.py", "seed_catalog_benchmark",
            "--ownership-record", "/run/benchmark/ownership.json",
            "--password-file", "/run/benchmark/password",
        ], mounts={
            str(self.workspace.path / "ownership.json"): "/run/benchmark/ownership.json",
            str(self.secret_path("bench-user-password")): "/run/benchmark/password",
        })
        if result.returncode:
            self.log("benchmark seed failed: " + _bounded(result.stderr, self.secrets))
            raise BenchmarkResourceError("benchmark seed failed")
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(summary, dict) or summary.get("token") != self.token:
            raise BenchmarkResourceError("benchmark seed summary is invalid")
        return summary

    def query_evidence(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Real middleware/view SQL counts and sanitized plans, as the aegis_web login."""
        result = self.run_oneoff(
            "web", ["python", "-m", "scripts.benchmarks.report", "query-evidence"],
            mounts={str(self.secret_path("bench-user-password")): "/run/benchmark/password"},
            environment={"AEGIS_ALLOWED_HOSTS": "testserver"},
            stdin=json.dumps({**spec, "passwordFile": "/run/benchmark/password"}),
            timeout=1800,
        )
        if result.returncode:
            self.log("benchmark query evidence failed: " + _bounded(result.stderr, self.secrets))
            raise BenchmarkResourceError("benchmark query evidence failed")
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(evidence, dict):
            raise BenchmarkResourceError("benchmark query evidence is invalid")
        return evidence

    def wait_healthy(self, services: Iterable[str], deadline_seconds: int = 300) -> None:
        ids = self.container_ids()
        deadline = time.monotonic() + deadline_seconds
        for service in services:
            while True:
                inspected = self.engine("container", "inspect", ids[service])
                state: dict[str, Any] = {}
                with contextlib.suppress(ValueError, IndexError, KeyError, TypeError):
                    state = json.loads(inspected.stdout)[0]["State"]
                health = (state.get("Health") or {}).get("Status")
                if state.get("Running") and health in ("healthy", None):
                    break
                if time.monotonic() >= deadline:
                    raise BenchmarkResourceError("benchmark service did not become healthy")
                time.sleep(1)

    def restart(self, services: Iterable[str]) -> None:
        """Restart exact recorded containers (cold run); identities must not change."""
        self.require_recorded()
        ids = self.container_ids()
        for service in services:
            if self.engine("restart", "--time", "30", ids[service], timeout=180).returncode:
                raise BenchmarkResourceError("benchmark service restart failed")
            self.wait_healthy([service])
        self.require_recorded()


def _bounded(text: str, secret_values: Iterable[str]) -> str:
    text = text[-4000:]
    for value in secret_values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _mounts_config(workspace: Workspace) -> bytes:
    lines = ["version = 1"]
    for root in ROOTS:
        source = workspace.path / "roots" / root.slot_id
        lines.extend([
            "", "[[slots]]", f'slot_id = "{root.slot_id}"',
            f"source = {json.dumps(str(source))}",
            f'container_path = "/srv/aegis/roots/{root.slot_id}"', 'mode = "read_only"',
            f"expected_identity = {json.dumps(local_identity(source))}",
        ])
    return ("\n".join(lines) + "\n").encode("ascii")


def cpu_placement(env: Mapping[str, str]) -> tuple[str, object]:
    """The reference shared four-CPU set, or (no cpuset controller) a 4-CPU quota each."""
    result = _run(container_command("info", "--format", "{{json .CPUSet}}", environment=env),
                  env, timeout=60)
    if result.returncode == 0 and result.stdout.strip() == "true" and (os.cpu_count() or 0) >= 4:
        return ("cpuset", CPUSET)
    return ("cpus", 4.0)


def _limits_override(profile: Mapping[str, Any], cpu: tuple[str, object]) -> bytes:
    services: dict[str, Any] = {}
    for role, mib in profile["memoryLimitsMiB"].items():
        services[role] = {"mem_limit": f"{mib}m", "memswap_limit": f"{mib}m", cpu[0]: cpu[1]}
    return (json.dumps({"services": services}, indent=2) + "\n").encode("ascii")


def _ownership_record(token: str, database: str, profile: Mapping[str, Any], entries: int,
                      wide: int, hold_seconds: int) -> str:
    return json.dumps({
        "version": 1, "token": token, "database": database, "role": "aegis_migrator",
        "entries": entries, "wideFolder": wide, "seed": profile["seed"],
        "users": profile["users"], "scheduleHoldSeconds": hold_seconds,
    }, sort_keys=True)


def _checked(result: subprocess.CompletedProcess[str], message: str, env: BenchmarkEnvironment,
             ) -> subprocess.CompletedProcess[str]:
    if result.returncode:
        env.log(f"{message}: " + _bounded(result.stderr or result.stdout, env.secrets))
        raise BenchmarkResourceError(message)
    return result


@contextlib.contextmanager
def benchmark_environment(
    profile: Mapping[str, Any], report_dir: Path, *, entries: int, wide_folder: int,
    hold_seconds: int, inherited: Mapping[str, str] | None = None,
    log: Callable[[str], None] = print,
) -> Iterator[BenchmarkEnvironment]:
    """Create, yield and exactly tear down one owned benchmark stack."""
    report_dir = validate_report_dir(report_dir)
    token = secrets.token_hex(6)
    project = project_name(token)
    database = f"aegis_bench_{token}"
    workspace = Workspace.create()
    http_port, db_port = free_loopback_port(), free_loopback_port()
    env = benchmark_environment_variables(
        os.environ if inherited is None else inherited, work=workspace.path, project=project,
        http_port=http_port, db_port=db_port, database=database,
    )
    base_arguments = (
        "--env-file", "/dev/null", "--project-name", project,
        "--project-directory", str(REPOSITORY), "-f", "compose.yaml", "-f", "compose.test.yaml",
    )
    bench = BenchmarkEnvironment(
        profile, report_dir, workspace, project, token, database, env, base_arguments,
        http_port, db_port, log=log,
    )
    started = False
    empty: ProjectInventory | None = None
    try:
        workspace.directory("secrets")
        for name in SECRET_NAMES:
            workspace.secret(f"secrets/{name}", secrets.token_hex(32))
        workspace.directory("roots")
        for root in ROOTS:
            # Empty synthetic mounts: catalog mode never scans them.
            workspace.directory(f"roots/{root.slot_id}", mode=0o755)
        workspace.file("mounts.toml", _mounts_config(workspace))
        workspace.secret("ownership.json", _ownership_record(
            token, database, profile, entries, wide_folder, hold_seconds,
        ))
        bench.cpu = cpu_placement(env)
        workspace.file("compose.benchmark.yaml", _limits_override(profile, bench.cpu))
        if _within(report_dir, workspace.path):
            raise BenchmarkResourceError("report directory must be outside the workspace")
        empty = require_empty_project(project, env)
        aegisctl = shutil.which("aegisctl", path=str(Path(sys.executable).parent))
        if aegisctl is None:
            raise BenchmarkResourceError("aegisctl is unavailable")
        config, manifest = workspace.path / "mounts.toml", workspace.path / "mounts.manifest.json"
        _checked(_run([aegisctl, "mounts", "preflight", "--config", str(config),
                       "--manifest", str(manifest)], env, timeout=120),
                 "benchmark mount preflight failed", bench)
        workspace.adopt("mounts.manifest.json")
        _checked(_run([aegisctl, "mounts", "render", "--config", str(config), "--manifest",
                       str(manifest), "--output", str(workspace.path / "compose.mounts.yaml"),
                       "--gateway-attestation",
                       str(workspace.path / "mounts.gateway.attestation")], env, timeout=120),
                 "benchmark mount render failed", bench)
        workspace.adopt("compose.mounts.yaml")
        workspace.adopt("mounts.gateway.attestation")
        bench.compose_arguments = (
            *base_arguments, "-f", str(workspace.path / "compose.mounts.yaml"),
            "-f", str(workspace.path / "compose.benchmark.yaml"),
        )
        rendered = _checked(bench.compose("config", "--format", "json", timeout=120),
                            "benchmark Compose configuration failed", bench)
        check_compose_config(json.loads(rendered.stdout), project=project, profile=profile,
                             cpu=bench.cpu)
        log("Building benchmark images (pinned Dockerfiles, existing tags)...")
        _checked(bench.compose("build", timeout=3600), "benchmark image build failed", bench)
        workspace.validate()
        log(f"Starting owned benchmark project {project} on loopback port {http_port}...")
        started = True
        up = bench.compose("up", "--detach", "--wait", "--wait-timeout", "300", timeout=900)
        bench.inventory = record_resources(project, env, expected=empty,
                                           allowed_new=resource_rules(workspace.path.name))
        _checked(up, "benchmark stack did not become healthy", bench)
        bench.storage = bench.storage_check()
        log(f"Database storage: {bench.storage['freeBytes'] // 1024**3} GiB free "
            f"(required {bench.storage['requiredFreeBytes'] // 1024**3} GiB), rotational="
            f"{bench.storage['rotational']}.")
        yield bench
    finally:
        failure: BaseException | None = None
        if started:
            try:
                inventory = bench.inventory
                if inventory is None and empty is not None:
                    inventory = record_resources(project, env, expected=empty,
                                                 allowed_new=resource_rules(workspace.path.name))
                if inventory is not None:
                    cleanup_resources(inventory, env)
                    log(f"Removed exactly {len(inventory.resources)} recorded benchmark resources "
                        f"of project {project}.")
            except (BenchmarkResourceError, ProjectResourceError) as exc:
                failure = exc
                log("Benchmark resource cleanup refused; resources and workspace preserved.")
        if failure is None:
            try:
                workspace.cleanup()
                log("Removed the recorded benchmark workspace; reports preserved.")
            except (BenchmarkResourceError, OSError) as exc:
                failure = exc
                log("Benchmark workspace cleanup refused; workspace preserved.")
        if failure is not None:
            original = sys.exc_info()[1]
            if original is not None:
                # Keep the run's own failure primary; the cleanup refusal is attached.
                log(f"Benchmark cleanup also refused ({type(failure).__name__}: {failure}).")
                original.add_note(f"benchmark cleanup also refused: {failure}")
            else:
                raise failure
