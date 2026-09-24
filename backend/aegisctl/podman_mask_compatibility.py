"""Fresh behavioral admission for Podman's duplicate powercap mask.

Raw engine constructors deliberately remain outside this runtime gate.
"""

from __future__ import annotations

import collections
import hashlib
import http.client
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import Path
from types import FrameType
from typing import Any

from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    local_environment,
    podman_compose_provider,
    podman_socket,
    selected_engine,
)
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
    require_project_inventory,
)

POWERCAP = "/sys/devices/virtual/powercap"
MASK_OPTION = f"unmask={POWERCAP}"
PROBE_IMAGE = "aegis-backend"
LIMIT = 4 * 1024 * 1024
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ANSIC_BUILD_TIME = re.compile(
    r"(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?P<day> [1-9]|[12][0-9]|3[01]) "
    r"(?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9]):(?P<second>[0-5][0-9]) "
    r"(?P<year>[0-9]{4})"
)


class PodmanMaskError(RuntimeError):
    """The measured compatibility policy could not be proved safely."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PodmanMaskError(message)


class _TerminationHandler:
    """Defer the first TERM only until a subprocess or timer startup is owned."""

    def __init__(self) -> None:
        self.deferred = False
        self.interrupted = False
        self.pending = False

    def __call__(self, number: int, frame: FrameType | None) -> None:
        if not self.interrupted:
            self.interrupted = True
            if self.deferred:
                self.pending = True
            else:
                raise SystemExit(128 + number)

    def resume(self) -> None:
        self.deferred = False
        if self.pending:
            self.pending = False
            raise SystemExit(128 + signal.SIGTERM)


def _run(
    command: list[str],
    environment: Mapping[str, str],
    *,
    timeout: int = 30,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bound pipes, wall time and the entire provider subprocess group."""
    handler = signal.getsignal(signal.SIGTERM)
    boundary = (
        handler
        if isinstance(handler, _TerminationHandler)
        and threading.current_thread() is threading.main_thread()
        else None
    )
    if boundary is not None:
        boundary.deferred = True
    try:
        process = subprocess.Popen(
            command,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
        )
        with process:
            assert process.stdout is not None and process.stderr is not None
            output = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
            try:
                if boundary is not None:
                    boundary.resume()
                with selectors.DefaultSelector() as selector:
                    for descriptor in output:
                        selector.register(descriptor, selectors.EVENT_READ)
                    deadline = time.monotonic() + timeout
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command[0], timeout)
                        for key, _ in selector.select(remaining):
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fd)
                            else:
                                output[key.fd].extend(data)
                                _require(
                                    sum(map(len, output.values())) <= LIMIT,
                                    "probe output exceeded bound",
                                )
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                output[process.stdout.fileno()].decode("utf-8"),
                output[process.stderr.fileno()].decode("utf-8"),
            )

    finally:
        if boundary is not None:
            boundary.resume()


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=10)
        self.path = path
        self.deadline = time.monotonic() + 10

    def connect(self) -> None:
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0, "Podman API identity deadline exceeded")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(remaining)
        self.sock.connect(str(self.path))
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0, "Podman API identity deadline exceeded")
        self.sock.settimeout(remaining)


def _api_json(path: Path, endpoint: str) -> dict[str, Any]:
    _require(endpoint in ("/v5.8.7/libpod/info", "/version"), "unsupported API query")
    deadline = time.monotonic() + 10
    connection = _UnixConnection(path)
    connection.deadline = deadline
    connection_socket: socket.socket | None = None

    def expire() -> None:
        # Shutdown wakes a blocked request/header/body operation even if it keeps
        # receiving fragments within each individual socket inactivity timeout.
        active = connection_socket if connection_socket is not None else connection.sock
        if active is not None:
            with suppress(OSError):
                active.shutdown(socket.SHUT_RDWR)

    timer: threading.Timer | None = None
    started = False
    primary: BaseException | None = None
    handler = signal.getsignal(signal.SIGTERM)
    boundary = (
        handler
        if isinstance(handler, _TerminationHandler)
        and threading.current_thread() is threading.main_thread()
        else None
    )
    try:
        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        try:
            # Do not let TERM interrupt Thread.start between spawning its thread
            # and publishing joinable state. Cleanup is already protected here.
            if boundary is not None:
                boundary.deferred = True
            timer.start()
            started = True
        finally:
            if boundary is not None:
                boundary.resume()
        connection.request("GET", endpoint)
        _require(time.monotonic() < deadline, "Podman API identity deadline exceeded")
        connection_socket = connection.sock
        _require(connection_socket is not None, "API connection missing")
        response = connection.getresponse()
        _require(time.monotonic() < deadline, "Podman API identity deadline exceeded")
        _require(response.status == 200, "Podman API identity query refused")
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            _require(remaining > 0, "Podman API identity deadline exceeded")
            assert connection_socket is not None
            connection_socket.settimeout(remaining)
            part = response.read1(min(65536, LIMIT + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
            _require(len(raw) <= LIMIT, "Podman API identity exceeded bound")
            if response.isclosed():
                break
        _require(time.monotonic() < deadline, "Podman API identity deadline exceeded")
        result = json.loads(raw)
        _require(isinstance(result, dict), "invalid API identity")
        return dict(result)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup = [connection.close]
        if timer is not None:
            # start() can fail before spawning, or raise after a thread starts.
            cleanup = [
                timer.cancel,
                *([timer.join] if started or timer.ident is not None else []),
                *cleanup,
            ]
        failures: list[BaseException] = []
        for action in cleanup:
            try:
                action()
            except BaseException as exc:
                failures.append(exc)
        if failures:
            if isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise primary from failures[0]
            raise failures[0]


def _selinux_enforcing() -> bool:
    return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"


def _file_identity(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode, value.st_nlink)


def _executable_identity(path: Path) -> tuple[object, ...]:
    resolved = path.resolve(strict=True)
    value = resolved.stat()
    _require(
        stat.S_ISREG(value.st_mode) and value.st_mode & 0o111 and not value.st_mode & 0o022,
        "unsafe executable identity",
    )
    before = _file_identity(resolved)
    with resolved.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    _require(before == _file_identity(resolved), "executable replaced during identity query")
    return (str(resolved), *before, value.st_size, value.st_mtime_ns, digest)


def _json_command(arguments: tuple[str, ...], environment: Mapping[str, str]) -> Any:
    result = _run(
        container_command(*arguments, environment=environment), local_environment(environment)
    )
    _require(result.returncode == 0, "native identity query refused")
    return json.loads(result.stdout)


def _validate_build_version(version: Any) -> None:
    _require(isinstance(version, dict), "invalid Podman build version")
    built = version.get("Built")
    display = version.get("BuiltTime")
    _require(
        type(built) is int and -(2**63) <= built < 2**63,
        "invalid Podman build timestamp",
    )
    _require(type(display) is str and len(display) == 24, "invalid Podman build display")
    match = _ANSIC_BUILD_TIME.fullmatch(display)
    _require(match is not None, "invalid Podman build display")
    assert match is not None
    try:
        calendar_date = date(
            int(match["year"]),
            _MONTHS.index(match["month"]) + 1,
            int(match["day"]),
        )
    except ValueError as exc:
        raise PodmanMaskError("invalid Podman build display") from exc
    _require(
        _WEEKDAYS[calendar_date.weekday()] == match["weekday"],
        "invalid Podman build display",
    )


def _without_build_display(identity: dict[str, Any]) -> dict[str, Any]:
    return identity | {
        "version": {key: value for key, value in identity["version"].items() if key != "BuiltTime"}
    }


def _info_identity(info: dict[str, Any]) -> dict[str, Any]:
    host = info["host"]
    version = info["version"]
    _validate_build_version(version)
    runtime = host["ociRuntime"]
    _require(
        version["Version"] == "5.8.7" and version["APIVersion"] == "5.8.7",
        "unsupported Podman version",
    )
    _require(
        host["security"]["rootless"] is True
        and host["security"]["selinuxEnabled"] is True
        and host["security"]["seccompEnabled"] is True,
        "unsupported rootless security state",
    )
    _require(
        runtime["name"] == "crun" and runtime["version"].splitlines()[0] == "crun version 1.28",
        "unsupported OCI runtime",
    )
    _require(host["os"] == "linux" and host["arch"] == "amd64", "unsupported probe architecture")
    return {
        "host": {
            key: host[key]
            for key in (
                "arch",
                "os",
                "hostname",
                "kernel",
                "cgroupVersion",
                "cgroupManager",
                "ociRuntime",
                "security",
                "idMappings",
            )
        },
        "store": {
            key: info["store"][key]
            for key in (
                "graphRoot",
                "runRoot",
                "graphDriverName",
                "volumePath",
            )
        },
        "version": version,
    }


def _identity(environment: Mapping[str, str]) -> dict[str, Any]:
    socket_path = podman_socket(environment)
    socket_before = _file_identity(socket_path)
    provider = podman_compose_provider(environment)
    executable = shutil.which("podman", path=environment.get("PATH"))
    _require(executable, "local Podman executable missing")
    native = _info_identity(_json_command(("info", "--format", "json"), environment))
    version = _json_command(("version", "--format", "json"), environment)["Client"]
    _validate_build_version(version)
    _require(version == native["version"], "native engine identity mismatch")
    api = _info_identity(_api_json(socket_path, "/v5.8.7/libpod/info"))
    _require(
        _without_build_display(api) == _without_build_display(native),
        "native and Unix API engine identities differ",
    )
    api_version = _api_json(socket_path, "/version")
    engines = [item for item in api_version["Components"] if item["Name"] == "Podman Engine"]
    runtimes = [item for item in api_version["Components"] if item["Name"] == "OCI Runtime (crun)"]
    _require(len(engines) == len(runtimes) == 1, "ambiguous API components")
    _require(
        engines[0]["Version"] == version["Version"]
        and engines[0]["Details"]["APIVersion"] == version["APIVersion"]
        and engines[0]["Details"]["GitCommit"] == version["GitCommit"]
        and api_version["Version"] == version["Version"]
        and api_version["GitCommit"] == version["GitCommit"]
        and api_version["ApiVersion"] == "1.44"
        and runtimes[0]["Version"] == native["host"]["ociRuntime"]["version"],
        "Unix API version or runtime mismatch",
    )
    provider_identity = _executable_identity(provider)
    provided = _run([str(provider), "version", "--short"], compose_environment(environment))
    _require(
        provided.returncode == 0 and provided.stdout.strip().removeprefix("v") == "2.39.4",
        "unsupported Docker Compose provider",
    )
    images = _json_command(("image", "inspect", PROBE_IMAGE), environment)
    _require(isinstance(images, list) and len(images) == 1, "approved local probe image missing")
    image = images[0]
    image_id = image["Id"].removeprefix("sha256:")
    _require(
        re.fullmatch("[0-9a-f]{64}", image_id)
        and image["Architecture"] == "amd64"
        and image["Os"] == "linux",
        "invalid immutable local probe image",
    )
    _require(_selinux_enforcing(), "probe requires enforcing SELinux")
    _require(socket_before == _file_identity(podman_socket(environment)), "API socket replaced")
    # Only a digest of environment routing/configuration is diagnostic data.
    environment_identity = {
        key: value
        for key, value in environment.items()
        if key.startswith(("CONTAINERS_", "XDG_"))
        or key in ("PATH", "HOME", "PODMAN_COMPOSE_PROVIDER", "AEGIS_PODMAN_SOCKET")
    }
    return {
        "native": native,
        "image": image_id,
        "socket": socket_before,
        "engine": _executable_identity(Path(str(executable))),
        "runtime": _executable_identity(Path(native["host"]["ociRuntime"]["path"])),
        "provider": provider_identity,
        "environmentDigest": hashlib.sha256(
            json.dumps(environment_identity, sort_keys=True).encode()
        ).hexdigest(),
    }


class _Diagnostics:
    def __init__(self, environment: Mapping[str, str], originals: tuple[Path, ...]) -> None:
        from aegisctl.mounts import ensure_outputs_outside_originals

        parent = Path(
            next(
                (environment[name] for name in ("TMPDIR", "TEMP", "TMP") if environment.get(name)),
                str(tempfile.tempdir or "/tmp"),
            )
        )
        if originals:
            ensure_outputs_outside_originals((parent,), originals)
        self.root = Path(tempfile.mkdtemp(prefix="aegis-mask-check-", dir=parent))
        self.identities = {self.root: _file_identity(self.root)}
        self.report = self.root / "evidence.jsonl"
        self.descriptor = os.open(
            self.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self.identities[self.report] = _file_identity(self.report)

    def validate(self) -> None:
        _require(
            {self.root, *self.root.iterdir()} == set(self.identities),
            "diagnostic inventory changed",
        )
        for path, identity in self.identities.items():
            _require(_file_identity(path) == identity, "diagnostic identity changed")

    def record(self, event: str, **fields: object) -> None:
        self.validate()
        data = (json.dumps({"event": event, **fields}, sort_keys=True) + "\n").encode()
        _require(len(data) <= LIMIT, "diagnostic record exceeded bound")
        while data:
            written = os.write(self.descriptor, data)
            _require(written > 0, "diagnostic write failed")
            data = data[written:]
        os.fsync(self.descriptor)

    def configuration(self, profile: str, service: dict[str, object]) -> Path:
        path = self.root / f"{profile}.json"
        with path.open("x") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump({"services": {"probe": service}}, target)
        self.identities[path] = _file_identity(path)
        return path

    def remove_configurations(self) -> None:
        self.validate()
        for path in list(self.identities):
            if path not in (self.root, self.report):
                path.unlink()
                del self.identities[path]


_PAYLOAD = r"""
import json, os, re
from aegisctl.mounts import MAX_MOUNTINFO_BYTES, parse_mountinfo
names = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')
with open('/proc/self/status', 'rb') as source:
    status = source.read(65537)
assert len(status) <= 65536
capabilities = {}
for line in status.decode('ascii').splitlines():
    name, separator, value = line.partition(':')
    if name in names:
        value = value.strip()
        assert separator and name not in capabilities
        assert re.fullmatch(r'[0-9a-fA-F]{16}', value) and int(value, 16) == 0
        capabilities[name] = value
assert set(capabilities) == set(names)
with open('/proc/self/mountinfo', 'rb') as source:
    raw = source.read(MAX_MOUNTINFO_BYTES + 1)
assert len(raw) <= MAX_MOUNTINFO_BYTES
error = None
try:
    parse_mountinfo(raw)
except Exception as exc:
    error = str(exc)
try:
    with os.scandir('/sys/devices/virtual/powercap') as entries:
        empty = next(entries, None) is None
except PermissionError:
    empty = True
print(json.dumps({'rows': raw.decode('ascii').splitlines(), 'parseError': error,
                 'powercapEmptyOrDenied': empty, 'uid': os.getuid(), 'gid': os.getgid(),
                 'capabilities': capabilities}))
"""


def _owner_inventory(owner: str, environment: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for kind in ("container", "network", "volume"):
        arguments = [
            kind,
            "ls",
            *(["--all", "--no-trunc"] if kind == "container" else []),
            "--quiet",
            "--filter",
            f"label=aegis.verify.owner={owner}",
        ]
        queried = _run(
            container_command(*arguments, environment=environment), local_environment(environment)
        )
        _require(queried.returncode == 0, "probe owner query refused")
        handles = queried.stdout.split()
        _require(len(handles) == len(set(handles)), "ambiguous owner scope")
        result[kind] = tuple(sorted(handles))
    return result


def _owned_scope(owner: str, inventory: ProjectInventory, environment: Mapping[str, str]) -> None:
    owned = _owner_inventory(owner, environment)
    _require(
        owned
        == {
            kind: tuple(
                sorted(
                    resource.immutable_id
                    for resource in inventory.resources
                    if resource.kind == kind
                )
            )
            for kind in ("container", "network", "volume")
        },
        "probe ownership scope differs",
    )


def _inspect(cid: str, image: str, environment: Mapping[str, str]) -> dict[str, Any]:
    values = _json_command(("container", "inspect", cid), environment)
    _require(isinstance(values, list) and len(values) == 1, "ambiguous probe identity")
    info = dict(values[0])
    host = info["HostConfig"]
    _require(
        info["Id"] == cid and info["Image"].removeprefix("sha256:") == image,
        "probe image or identity changed",
    )
    _require(
        info["Config"]["User"] == "10001:10001"
        and host["ReadonlyRootfs"] is True
        and host["NetworkMode"] == "none"
        and host["Privileged"] is False,
        "unsafe probe boundary",
    )
    # Podman 5.8.7 serializes nil []string inspect fields as null. This is
    # representation validation only; initialized/final OCI and kernel checks
    # independently prove that all capability sets are empty.
    _require(
        all(
            name in info
            and (info[name] is None or (type(info[name]) is list and len(info[name]) == 0))
            for name in ("EffectiveCaps", "BoundingCaps")
        ),
        "invalid inspect capability fields",
    )
    _require(
        "no-new-privileges" in host["SecurityOpt"]
        and not any(
            "unconfined" in option or "label=disable" in option for option in host["SecurityOpt"]
        )
        and info["ProcessLabel"]
        and info["MountLabel"],
        "probe confinement absent",
    )
    _require(
        host["Memory"] == 64 * 1024 * 1024
        and host["PidsLimit"] == 64
        and (
            host.get("NanoCpus") == 500_000_000
            or (host.get("CpuPeriod", 0) > 0 and host.get("CpuQuota") == host["CpuPeriod"] / 2)
        ),
        "probe resource limits differ",
    )
    _require(
        not any(
            host.get(key)
            for key in ("Binds", "Devices", "DeviceRequests", "PortBindings", "VolumesFrom")
        )
        and not info.get("Secrets")
        and not info["Config"].get("Volumes")
        and all(mount["Type"] == "tmpfs" for mount in info.get("Mounts", [])),
        "probe contains external inputs",
    )
    return info


def _oci(info: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    path = Path(info["OCIConfigPath"])
    roots = [Path(identity["native"]["store"][key]) for key in ("graphRoot", "runRoot")]
    _require(
        path.is_absolute() and any(path.resolve().is_relative_to(root.resolve()) for root in roots),
        "OCI path outside engine storage",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid(), "unsafe OCI file"
        )
        raw = os.read(descriptor, LIMIT + 1)
        _require(len(raw) <= LIMIT, "OCI specification exceeded bound")
        spec = json.loads(raw)
    finally:
        os.close(descriptor)
    process = spec.get("process")
    _require(isinstance(process, dict), "missing OCI capability process")
    capabilities = process.get("capabilities")
    _require(isinstance(capabilities, dict), "missing OCI capability object")
    names = ("bounding", "effective", "inheritable", "permitted", "ambient")
    _require(
        set(capabilities) <= set(names)
        and all(type(value) is list and len(value) == 0 for value in capabilities.values()),
        "unsafe OCI capability sets",
    )
    # The tagged OCI Go fields use omitempty: absent members are empty only
    # within the explicitly present capabilities object validated above.
    result = {
        "capabilities": {name: [] for name in names},
        "masks": spec["linux"]["maskedPaths"],
        "readonly": spec["linux"]["readonlyPaths"],
        "rootReadonly": spec["root"]["readonly"],
        "nnp": spec["process"]["noNewPrivileges"],
    }
    _require(result["rootReadonly"] is True and result["nnp"] is True, "OCI protection missing")
    _require(
        all(
            isinstance(value, str) and value.startswith("/")
            for values in (result["masks"], result["readonly"])
            for value in values
        ),
        "invalid OCI paths",
    )
    return result


def _compare_oci(observed: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    masks = collections.Counter(observed["masks"])
    if baseline is None:
        _require(masks[POWERCAP] == 2, "unproved baseline powercap behavior")
    else:
        expected = collections.Counter(baseline["masks"])
        _require(expected[POWERCAP] == 2, "invalid baseline evidence")
        expected[POWERCAP] -= 1
        _require(
            masks == expected and observed["readonly"] == baseline["readonly"],
            "candidate changed effective OCI protections",
        )


def _mount_evidence(raw: str, candidate: bool) -> dict[str, Any]:
    from aegisctl.mounts import MAX_MOUNTINFO_BYTES, MountAttestationError, parse_mountinfo

    _require(len(raw.encode()) <= 262144, "payload exceeded bound")
    evidence = json.loads(raw)
    capabilities = evidence.get("capabilities")
    _require(
        isinstance(capabilities, dict)
        and set(capabilities) == {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-fA-F]{16}", value)
            and int(value, 16) == 0
            for value in capabilities.values()
        ),
        "unproved runtime capability bitmaps",
    )
    _require(
        evidence["uid"] == evidence["gid"] == 10001 and evidence["powercapEmptyOrDenied"] is True,
        "powercap exposure or wrong user",
    )
    rows = evidence["rows"]
    mountinfo = ("\n".join(rows) + "\n").encode("ascii")
    _require(len(mountinfo) <= MAX_MOUNTINFO_BYTES, "mountinfo exceeded bound")
    if candidate:
        records = parse_mountinfo(mountinfo)
        _require(
            evidence["parseError"] is None
            and records[POWERCAP].effective_mode == "read_only"
            and str(records[POWERCAP].filesystem_root) == "/crun/.empty-directory"
            and records["/"].effective_mode == "read_only",
            "candidate runtime mask absent",
        )
    else:
        counts = collections.Counter(row.split()[4] for row in rows)
        _require(
            {key: value for key, value in counts.items() if value > 1} == {POWERCAP: 2},
            "baseline duplicate behavior differs",
        )
        try:
            parse_mountinfo(mountinfo)
        except MountAttestationError as exc:
            _require(
                str(exc) == evidence["parseError"] == "mountinfo contains an ambiguous mountpoint",
                "baseline parser behavior differs",
            )
        else:
            raise PodmanMaskError("baseline parser unexpectedly accepted duplicate mask")
    return dict(evidence)


def _profile(
    transport: str,
    candidate: bool,
    baseline: dict[str, Any] | None,
    identity: dict[str, Any],
    environment: Mapping[str, str],
    diagnostics: _Diagnostics,
) -> dict[str, Any]:
    profile = f"{transport}-{'candidate' if candidate else 'baseline'}"
    owner = f"aegis-mask-{uuid.uuid4().hex}"
    image = identity["image"]
    before = require_empty_project(owner, environment, runner=_run)
    _owned_scope(owner, before, environment)
    diagnostics.record("empty-scope", profile=profile, owner=owner)
    options = ["no-new-privileges:true", *([MASK_OPTION] if candidate else [])]
    if transport == "compose":
        path = diagnostics.configuration(
            profile,
            {
                "image": image,
                "pull_policy": "never",
                "container_name": owner,
                "labels": {"aegis.verify.owner": owner},
                "network_mode": "none",
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": options,
                "mem_limit": "64m",
                "cpus": 0.5,
                "pids_limit": 64,
                "entrypoint": ["python"],
                "command": ["-c", _PAYLOAD],
                "restart": "no",
                "stop_grace_period": "1s",
            },
        )
        command = compose_command(
            "-p", owner, "-f", str(path), "create", "probe", environment=environment
        )
        create_environment = compose_environment(environment)
    else:
        command = container_command(
            "create",
            "--name",
            owner,
            "--label",
            f"aegis.verify.owner={owner}",
            "--label",
            f"com.docker.compose.project={owner}",
            "--pull",
            "never",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            options[0],
            *(["--security-opt", MASK_OPTION] if candidate else []),
            "--memory",
            "64m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--timeout",
            "10",
            "--entrypoint",
            "python",
            image,
            "-c",
            _PAYLOAD,
            environment=environment,
        )
        create_environment = local_environment(environment)
    expected: ProjectInventory | None = None
    primary: BaseException | None = None
    result: dict[str, Any] = {}
    try:
        create_error: BaseException | None = None
        created: subprocess.CompletedProcess[str] | None = None
        try:
            created = _run(command, create_environment)
        except BaseException as exc:
            create_error = exc
        try:
            rules = (
                ProjectResourceRule(
                    "container",
                    (
                        ("aegis.verify.owner", owner),
                        *(
                            ((("com.docker.compose.service", "probe"),))
                            if transport == "compose"
                            else ()
                        ),
                    ),
                    name=owner,
                ),
            )
            admitted = admit_project_transition(
                before, capture_project_inventory(owner, environment, runner=_run), rules
            )
            for resource in admitted.resources:
                fields = json.loads(resource.fingerprint)
                _require(
                    re.fullmatch("[0-9a-f]{64}", resource.immutable_id)
                    and fields["Image"].removeprefix("sha256:") == image,
                    "recovered probe image or canonical identity differs",
                )
            _owned_scope(owner, admitted, environment)
            expected = admitted
            diagnostics.record(
                "create-recovered",
                profile=profile,
                identities=[resource.immutable_id for resource in expected.resources],
            )
        except BaseException as recovery:
            if isinstance(create_error, (KeyboardInterrupt, SystemExit)):
                raise create_error from recovery
            raise
        if create_error is not None:
            raise create_error
        _require(created is not None and created.returncode == 0, "probe create refused")
        _require(len(expected.resources) == 1, "probe create produced no exact identity")
        cid = expected.resources[0].immutable_id
        _require(re.fullmatch("[0-9a-f]{64}", cid), "noncanonical probe identity")
        _inspect(cid, image, environment)
        initialized = _run(
            container_command("init", cid, environment=environment), local_environment(environment)
        )
        _require(initialized.returncode == 0, "probe init refused")
        initial = _oci(_inspect(cid, image, environment), identity)
        diagnostics.record("initialized-oci", profile=profile, **initial)
        _compare_oci(initial, baseline)
        executed = _run(
            container_command("start", "--attach", cid, environment=environment),
            local_environment(environment),
            timeout=10,
        )
        _require(executed.returncode == 0, "probe payload refused")
        observed = _mount_evidence(executed.stdout, candidate)
        final = _oci(_inspect(cid, image, environment), identity)
        _require(final == initial, "OCI protections changed during execution")
        diagnostics.record("observed", profile=profile, oci=final, mountinfo=observed)
        result = final
    except BaseException as exc:
        primary = exc
    try:
        diagnostics.validate()
        _require(expected is not None, "probe creation state uncertain")
        assert expected is not None
        _owned_scope(owner, expected, environment)
        require_project_inventory(expected, environment, runner=_run)
        cleanup_project_inventory(expected, environment, runner=_run)
        _owned_scope(owner, require_empty_project(owner, environment, runner=_run), environment)
        diagnostics.record("cleanup-confirmed", profile=profile)
    except BaseException as recovery:
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary from recovery
        raise
    if primary is not None:
        raise primary
    return result


@contextmanager
def _termination_boundary() -> Iterator[None]:
    """Own SIGTERM only with the default caller disposition, then restore it.

    Repeat TERM signals are deferred once recovery starts so they cannot interrupt
    exact-owned cleanup. A blocked signal, custom/ignored caller disposition or worker thread is
    refused before diagnostics or resources, rather than silently taking it over.
    """
    _require(
        threading.current_thread() is threading.main_thread(), "mask gate requires main thread"
    )
    _require(
        signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, set()),
        "mask gate requires unblocked SIGTERM",
    )
    previous = signal.getsignal(signal.SIGTERM)
    _require(previous == signal.SIG_DFL, "mask gate requires default SIGTERM disposition")
    primary: BaseException | None = None
    try:
        signal.signal(signal.SIGTERM, _TerminationHandler())
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            signal.signal(signal.SIGTERM, previous)
        except BaseException as recovery:
            if isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise primary from recovery
            raise


def require_podman_mask_compatibility(
    environment: Mapping[str, str] | None = None,
    *,
    originals: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    """Prove the exact supported behavior before any original-bearing launch."""
    source = os.environ if environment is None else environment
    # Docker must neither inspect an image nor create even diagnostic files.
    if source.get("AEGIS_CONTAINER_ENGINE", "docker") == "docker":
        selected_engine(source)
        return ()
    with _termination_boundary():
        diagnostics: _Diagnostics | None = None
        try:
            diagnostics = _Diagnostics(source, originals)
            diagnostics.record("begin", pid=os.getpid())
            selected_engine(source)
            identity = _identity(source)
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            diagnostics.record("identity", identity=identity, digest=digest)
            # Reported identities do not close over every effective containers.conf
            # input. Require fresh behavior instead of reusing a prior success.
            evidence: dict[str, dict[str, Any]] = {}
            for transport in ("native", "compose"):
                baseline = _profile(transport, False, None, identity, source, diagnostics)
                if evidence:
                    _require(
                        baseline == evidence["native"],
                        "native and Compose baseline protections differ",
                    )
                _profile(transport, True, baseline, identity, source, diagnostics)
                evidence[transport] = baseline
            _require(_identity(source) == identity, "identity changed during behavior check")
            diagnostics.remove_configurations()
            diagnostics.record("completed")
            return (MASK_OPTION,)
        except BaseException as exc:
            if diagnostics is not None:
                with suppress(Exception):
                    diagnostics.record("refused", errorType=type(exc).__name__)
            message = "Podman mask compatibility refused"
            if diagnostics is not None:
                message += f"; diagnostics retained at {diagnostics.root}"
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                exc.add_note(message)
                raise
            raise PodmanMaskError(message) from exc
        finally:
            if diagnostics is not None:
                os.close(diagnostics.descriptor)
