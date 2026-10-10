from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import secrets
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Literal, cast

# The host-side Podman deployment runs this module with only the standard library: the
# Compose paths import yaml lazily, where they use it.
from aegisctl.container_engine import (
    REMOTE_VARIABLES,
    compose_command,
    compose_environment,
)
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResourceError,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_empty_project,
)
from aegisctl.host_mounts import HostTopologyError, darwin_mountpoints, darwin_path_forms
from aegisctl.podman_mask_compatibility import (
    MASK_OPTION,
    PodmanMaskError,
    require_podman_mask_compatibility,
)

if TYPE_CHECKING:
    from aegis_apps.roots.manifest import MountManifest

Mode = Literal["read_only", "read_write"]

MAX_CONFIG_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SLOTS = 128
MAX_PATH_LENGTH = 4096
MAX_IDENTITY_LENGTH = 512
MAX_INTEGER = (1 << 63) - 1
MAX_MOUNTINFO_BYTES = 1024 * 1024
OBSERVER_TIMEOUT_SECONDS = 30
# Enough for an engine's error report; the rest is drained and dropped.
OBSERVER_STDERR_LIMIT = 64 * 1024
SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LOCAL_IDENTITY_RE = re.compile(r"^local:(0|[1-9][0-9]{0,18}):(0|[1-9][0-9]{0,18})$")
REMOTE_IDENTITY_RE = re.compile(
    r"^remote:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?:"
    r"/[A-Za-z0-9._~!$&'()+,;=:@%/-]{1,383}$"
)
MANIFEST_TARGET = "/run/aegis/mounts.manifest.json"
GATEWAY_ATTESTATION_TARGET = "/run/aegis/mounts.gateway.attestation"
# The Podman deployment (Quadlet units and its plain podman run observer).
UNIT_PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{0,30}-")
QUADLET_ROLES = ("migrate", "web", "operations", "indexer", "media", "gateway")
QUADLET_DROP_IN = "50-aegis-roots.conf"
MAX_GROUP_ID = 2**32 - 2
_IMAGE_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REGISTRY_PORT = (
    r"(?:6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}"
    r"|[1-9][0-9]{0,3})"
)
OBSERVER_IMAGE_RE = re.compile(
    rf"(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::{_REGISTRY_PORT})?/)?"
    rf"{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*@sha256:[0-9a-f]{{64}}"
)
LOCAL_IMAGE_ID_RE = re.compile(r"[0-9a-f]{64}")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
# Containers that read originals run as an Aegis reader type, or as plain container_t in
# the deployment test; nothing may lift or replace their confinement.
READER_TYPE_RE = re.compile(r"container_t|aegis_[a-z0-9_]{1,50}\.process")
OBSERVER_LABEL = "aegis.observer"
# The gateway image's fixed user, whose primary group maps to the operator's group (D5).
OBSERVER_USER = 101
_PODMAN = ("podman", "--remote=false")
# Quadlet's Volume= syntax, systemd unit-file words with their specifier ("%") and variable
# ("$") expansion, and Podman's volume parser must all read a bind source literally.
_QUADLET_UNSAFE = frozenset(" \"'\\%$,:")
_QUADLET_EXEC_WORD_RE = re.compile(r"[A-Za-z0-9 ./_&=-]+")


class ConfigError(ValueError):
    """A bounded, safe mount configuration error."""


class MountAttestationError(ValueError):
    """A safe runtime mount-attestation error."""


@dataclass(frozen=True, slots=True)
class SlotSpec:
    slot_id: str
    source: Path
    container_path: str
    mode: Mode
    expected_identity: str


@dataclass(frozen=True, slots=True)
class ValidatedSlot:
    slot_id: str
    source: Path
    container_path: str
    mode: Mode
    filesystem_id: int
    root_inode: int
    expected_identity: str
    mount_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class RenderResult:
    manifest_digest: str
    gateway_digest: str


@dataclass(frozen=True, slots=True)
class _ManifestBinding:
    """The manifest fields a render binds to each configured slot."""

    container_path: str
    mode: Mode
    expected_identity: str
    filesystem_id: int
    root_inode: int
    mount_fingerprint: str


@dataclass(frozen=True, slots=True)
class MountInfoRecord:
    mountpoint: str
    effective_mode: Mode
    mount_fingerprint: str
    filesystem_identity: tuple[str, str]
    filesystem_root: PurePosixPath


@dataclass(frozen=True, slots=True)
class ObserverPathIdentity:
    device: int
    inode: int
    file_type: int
    uid: int
    gid: int
    links: int


def _safe_slot_error(slot_id: object, message: str) -> ConfigError:
    safe_id = slot_id if isinstance(slot_id, str) and SLOT_ID_RE.fullmatch(slot_id) else "invalid"
    return ConfigError(f"mount slot {safe_id}: {message}")


def _bounded_uint(value: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        raise ConfigError("invalid local identity")
    parsed = int(value)
    if parsed > MAX_INTEGER:
        raise ConfigError("invalid local identity")
    return parsed


def validate_expected_identity(identity: object) -> str:
    if not isinstance(identity, str) or not identity.isascii():
        raise ConfigError("invalid expected identity")
    if not 1 <= len(identity) <= MAX_IDENTITY_LENGTH:
        raise ConfigError("invalid expected identity")
    local = LOCAL_IDENTITY_RE.fullmatch(identity)
    if local:
        _bounded_uint(local.group(1))
        if _bounded_uint(local.group(2)) == 0:
            raise ConfigError("invalid expected identity")
        return identity
    if REMOTE_IDENTITY_RE.fullmatch(identity):
        return identity
    raise ConfigError("invalid expected identity")


def _parse_slot(item: object) -> SlotSpec:
    if not isinstance(item, dict) or set(item) != {
        "slot_id",
        "source",
        "container_path",
        "mode",
        "expected_identity",
    }:
        raise ConfigError("invalid mount slot schema")
    slot_id = item["slot_id"]
    if not isinstance(slot_id, str) or not SLOT_ID_RE.fullmatch(slot_id):
        raise _safe_slot_error(slot_id, "invalid slot ID")
    source_value = item["source"]
    if (
        not isinstance(source_value, str)
        or not source_value.isascii()
        or not 1 <= len(source_value) <= MAX_PATH_LENGTH
        or "\x00" in source_value
    ):
        raise _safe_slot_error(slot_id, "invalid source")
    source = Path(source_value)
    if not source.is_absolute():
        raise _safe_slot_error(slot_id, "source must be absolute")
    container_path = item["container_path"]
    expected_path = f"/srv/aegis/roots/{slot_id}"
    if container_path != expected_path:
        raise _safe_slot_error(slot_id, "invalid container path")
    mode_value = item["mode"]
    if mode_value not in ("read_only", "read_write"):
        raise _safe_slot_error(slot_id, "invalid mode")
    try:
        identity = validate_expected_identity(item["expected_identity"])
    except ConfigError as exc:
        raise _safe_slot_error(slot_id, str(exc)) from None
    return SlotSpec(slot_id, source, expected_path, cast(Mode, mode_value), identity)


def _read_bounded_regular(
    path: Path, limit: int, label: str,
    *, require: Callable[[os.stat_result], None] | None = None,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{label} is not a regular file")
        if require is not None:
            require(info)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except ConfigError:
        raise
    except OSError as exc:
        try:
            is_symlink = path.is_symlink()
        except OSError:
            is_symlink = False
        message = f"{label} is not a regular file" if is_symlink else f"{label} cannot be read"
        raise ConfigError(message) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > limit:
        raise ConfigError(f"{label} exceeds size limit")
    return raw


def _private_manifest_file(info: os.stat_result) -> None:
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigError("mount manifest has invalid permissions")
    if (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
        raise ConfigError("mount manifest has invalid owner")


def _manifest_integer(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ConfigError("invalid mount slot integer")
    return value


def _manifest_binding(item: object) -> tuple[str, _ManifestBinding]:
    if not isinstance(item, dict) or set(item) != {
        "slotId", "containerPath", "mode", "filesystemId", "rootInode",
        "expectedIdentity", "mountFingerprint",
    }:
        raise ConfigError("invalid mount slot schema")
    slot_id = item["slotId"]
    if not isinstance(slot_id, str) or not SLOT_ID_RE.fullmatch(slot_id):
        raise ConfigError("invalid mount slot ID")
    expected_path = f"/srv/aegis/roots/{slot_id}"
    if item["containerPath"] != expected_path:
        raise ConfigError(f"invalid container path for slot {slot_id}")
    mode = item["mode"]
    if mode not in ("read_only", "read_write"):
        raise ConfigError(f"invalid mode for slot {slot_id}")
    try:
        identity = validate_expected_identity(item["expectedIdentity"])
    except ConfigError:
        raise ConfigError(f"invalid identity for slot {slot_id}") from None
    filesystem_id = _manifest_integer(item["filesystemId"], allow_zero=True)
    root_inode = _manifest_integer(item["rootInode"], allow_zero=False)
    fingerprint = item["mountFingerprint"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ConfigError(f"invalid mount fingerprint for slot {slot_id}")
    if identity.startswith("local:") and identity != f"local:{filesystem_id}:{root_inode}":
        raise ConfigError(f"invalid identity for slot {slot_id}")
    return slot_id, _ManifestBinding(
        expected_path, cast(Mode, mode), identity, filesystem_id, root_inode, fingerprint,
    )


def _read_render_manifest(path: Path) -> tuple[str, Mapping[str, _ManifestBinding]]:
    """A standard-library mirror of aegis_apps.roots.manifest.MountManifest.load.

    The host-side Podman deployment has only this package, not the backend's Django apps.
    The manifest is read once, so its digest describes exactly the parsed bytes.
    """
    raw = _read_bounded_regular(
        path, MAX_MANIFEST_BYTES, "mount manifest", require=_private_manifest_file,
    )
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("invalid mount manifest JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "generatedAt", "slots"}:
        raise ConfigError("unsupported mount manifest schema")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ConfigError("unsupported mount manifest schema")
    generated = payload["generatedAt"]
    if not isinstance(generated, str) or len(generated) > 40 or not generated.endswith("Z"):
        raise ConfigError("invalid mount manifest timestamp")
    try:
        datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("invalid mount manifest timestamp") from exc
    items = payload["slots"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SLOTS:
        raise ConfigError("invalid mount manifest slot count")
    bindings: dict[str, _ManifestBinding] = {}
    identities: set[str] = set()
    observed: set[tuple[int, int]] = set()
    fingerprints: set[str] = set()
    for item in items:
        slot_id, binding = _manifest_binding(item)
        pair = (binding.filesystem_id, binding.root_inode)
        if (
            slot_id in bindings
            or binding.expected_identity in identities
            or pair in observed
            or binding.mount_fingerprint in fingerprints
        ):
            raise ConfigError(f"duplicate mount slot {slot_id}")
        bindings[slot_id] = binding
        identities.add(binding.expected_identity)
        observed.add(pair)
        fingerprints.add(binding.mount_fingerprint)
    return digest, MappingProxyType(bindings)


def parse_config(path: Path) -> tuple[SlotSpec, ...]:
    raw = _read_bounded_regular(path, MAX_CONFIG_BYTES, "mount config")
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("invalid mount config") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "slots"}:
        raise ConfigError("invalid mount config schema")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ConfigError("unsupported mount config schema")
    items = payload["slots"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SLOTS:
        raise ConfigError("invalid mount slot count")
    slots = tuple(_parse_slot(item) for item in items)
    ids = [slot.slot_id for slot in slots]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate mount slot ID")
    identities = [slot.expected_identity for slot in slots]
    if len(identities) != len(set(identities)):
        raise ConfigError("duplicate mount slot identity")
    return slots


def local_identity(path: Path) -> str:
    info = path.stat()
    return f"local:{info.st_dev}:{info.st_ino}"


def preflight_slots(slots: list[SlotSpec] | tuple[SlotSpec, ...]) -> tuple[ValidatedSlot, ...]:
    if not 1 <= len(slots) <= MAX_SLOTS:
        raise ConfigError("invalid mount slot count")
    resolved_slots: list[ValidatedSlot] = []
    seen_paths: set[Path] = set()
    seen_stat: set[tuple[int, int]] = set()
    seen_ids: set[str] = set()
    mount_records = _host_mountinfo()
    mountpoints = _host_mountpoints(mount_records)
    physical_roots: list[tuple[tuple[str, str], PurePosixPath]] = []
    for spec in sorted(slots, key=lambda item: item.slot_id):
        # Validate callers constructing SlotSpec directly as strictly as parsed config.
        checked = _parse_slot(
            {
                "slot_id": spec.slot_id,
                "source": str(spec.source),
                "container_path": spec.container_path,
                "mode": spec.mode,
                "expected_identity": spec.expected_identity,
            }
        )
        if checked.expected_identity in seen_ids:
            raise ConfigError(f"mount slot {checked.slot_id}: duplicate identity")
        seen_ids.add(checked.expected_identity)
        try:
            resolved = checked.source.resolve(strict=True)
            info = resolved.stat()
        except OSError as exc:
            raise ConfigError(f"mount slot {checked.slot_id}: source is inaccessible") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError(f"mount slot {checked.slot_id}: source is not a directory")
        if not os.access(resolved, os.R_OK | os.X_OK, effective_ids=True):
            raise ConfigError(f"mount slot {checked.slot_id}: source is inaccessible")
        identity_pair = (info.st_dev, info.st_ino)
        source_forms = _host_path_forms(resolved)
        _reject_nested_mounts(source_forms, mountpoints)
        if resolved in seen_paths or identity_pair in seen_stat:
            raise ConfigError(f"mount slot {checked.slot_id}: duplicate source")
        for existing in seen_paths:
            if any(candidate == existing or candidate in existing.parents
                   or existing in candidate.parents for candidate in source_forms):
                raise ConfigError(f"mount slot {checked.slot_id}: source overlap")
        if mount_records is not None:
            physical = _physical_location(resolved, mount_records)
            if any(_physical_overlap(physical, existing) for existing in physical_roots):
                raise ConfigError(f"mount slot {checked.slot_id}: physical source overlap")
            physical_roots.append(physical)
        seen_paths.update(source_forms)
        seen_stat.add(identity_pair)
        observed_identity = f"local:{info.st_dev}:{info.st_ino}"
        if checked.expected_identity.startswith("local:") and not secrets.compare_digest(
            checked.expected_identity, observed_identity
        ):
            raise ConfigError(f"mount slot {checked.slot_id}: identity mismatch")
        root_fd = -1
        try:
            root_fd = os.open(
                resolved,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            fd_info = os.fstat(root_fd)
            if (fd_info.st_dev, fd_info.st_ino) != identity_pair:
                raise ConfigError(f"mount slot {checked.slot_id}: source changed")
        except ConfigError:
            raise
        except OSError as exc:
            raise ConfigError(f"mount slot {checked.slot_id}: source is inaccessible") from exc
        finally:
            if root_fd >= 0:
                os.close(root_fd)
        resolved_slots.append(
            ValidatedSlot(
                checked.slot_id,
                resolved,
                checked.container_path,
                checked.mode,
                info.st_dev,
                info.st_ino,
                checked.expected_identity,
            )
        )
    return tuple(resolved_slots)


def runtime_identity() -> tuple[int, int]:
    raw_uid = os.environ.get("AEGIS_UID")
    raw_gid = os.environ.get("AEGIS_GID")
    if (raw_uid is None) != (raw_gid is None):
        raise ConfigError("AEGIS_UID and AEGIS_GID must both be set or both absent")
    current = (os.geteuid(), os.getegid())
    if raw_uid is None or raw_gid is None:
        values = current
    else:
        for value in (raw_uid, raw_gid):
            if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
                raise ConfigError("invalid runtime identity")
        values = (int(raw_uid), int(raw_gid))
    if not all(0 < value <= 2_147_483_647 for value in values):
        raise ConfigError("invalid runtime identity")
    if values != current:
        raise ConfigError("runtime identity must match invoking identity")
    return values


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    temp_name = f".{path.name}.tmp-{secrets.token_hex(16)}"
    temp_fd = -1
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        os.fchown(temp_fd, os.geteuid(), os.getegid())
        os.fchmod(temp_fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(temp_fd, view)
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=directory_fd)
        os.close(directory_fd)


def write_manifest(
    path: Path, slots: tuple[ValidatedSlot, ...], *, uid: int, gid: int
) -> str:
    if (uid, gid) != (os.geteuid(), os.getegid()) or uid == 0 or gid == 0:
        raise ConfigError("manifest identity must match invoking identity")
    if any(not re.fullmatch(r"[0-9a-f]{64}", slot.mount_fingerprint) for slot in slots):
        raise ConfigError("mount fingerprint is missing or invalid")
    ensure_outputs_outside_originals((path,), tuple(slot.source for slot in slots))
    payload = {
        "version": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "slots": [
            {
                "slotId": slot.slot_id,
                "containerPath": slot.container_path,
                "mode": slot.mode,
                "filesystemId": slot.filesystem_id,
                "rootInode": slot.root_inode,
                "expectedIdentity": slot.expected_identity,
                "mountFingerprint": slot.mount_fingerprint,
            }
            for slot in sorted(slots, key=lambda item: item.slot_id)
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    _atomic_write(path, raw, 0o600)
    return hashlib.sha256(raw).hexdigest()


def _paths_alias(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigError("output alias cannot be checked") from exc


def _ensure_distinct_artifacts(paths: tuple[Path, ...]) -> None:
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if _paths_alias(first, second):
                raise ConfigError("generated artifact alias is forbidden")


def ensure_outputs_outside_originals(paths: tuple[Path, ...], roots: tuple[Path, ...]) -> None:
    """Reject destinations before mkdir, replacement, or temporary-file creation."""
    try:
        boundaries = {root.resolve(strict=True) for root in roots}
        boundaries.update(Path(os.path.abspath(root)) for root in roots)
        records = _host_mountinfo()
        mountpoints = _host_mountpoints(records)
        for root in roots:
            forms = _host_path_forms(root.resolve(strict=True))
            _reject_nested_mounts(forms, mountpoints)
            boundaries.update(forms)
        physical_roots = ([_physical_location(root, records) for root in boundaries]
                          if records is not None else [])
        for path in paths:
            candidates = {Path(os.path.abspath(path)), path.resolve(strict=False)}
            candidates.update(_host_path_forms(path))
            if any(candidate == root or root in candidate.parents
                   for candidate in candidates for root in boundaries):
                raise ConfigError("generated artifacts must be outside original roots")
            if records is not None:
                physical = _physical_location(path.resolve(strict=False), records)
                if any(physical[0] == root[0] and physical[1].is_relative_to(root[1])
                       for root in physical_roots):
                    raise ConfigError("generated artifacts must be outside original roots")
    except OSError as exc:
        raise ConfigError("original output boundary cannot be checked") from exc


def _bind(source: Path | str, target: str, *, read_only: bool) -> dict[str, object]:
    return {
        "type": "bind",
        "source": str(source).replace("$", "$$"),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def _validated_render_slots(
    specs: tuple[SlotSpec, ...], bindings: Mapping[str, _ManifestBinding],
) -> tuple[ValidatedSlot, ...]:
    if set(bindings) != {spec.slot_id for spec in specs}:
        raise ConfigError("mount manifest does not match config")
    validated: list[ValidatedSlot] = []
    for spec in sorted(specs, key=lambda item: item.slot_id):
        binding = bindings.get(spec.slot_id)
        if binding is None:
            raise ConfigError(f"mount slot {spec.slot_id}: manifest mismatch")
        if (
            binding.container_path != spec.container_path
            or binding.mode != spec.mode
            or binding.expected_identity != spec.expected_identity
        ):
            raise ConfigError(f"mount slot {spec.slot_id}: manifest mismatch")
        try:
            resolved = spec.source.resolve(strict=True)
            info = resolved.stat()
        except OSError as exc:
            raise ConfigError(f"mount slot {spec.slot_id}: source is inaccessible") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError(f"mount slot {spec.slot_id}: source is not a directory")
        if (info.st_dev, info.st_ino) != (binding.filesystem_id, binding.root_inode):
            raise ConfigError(f"mount slot {spec.slot_id}: source identity changed")
        validated.append(
            ValidatedSlot(
                spec.slot_id,
                resolved,
                spec.container_path,
                spec.mode,
                info.st_dev,
                info.st_ino,
                spec.expected_identity,
                binding.mount_fingerprint,
            )
        )
    return tuple(validated)


def _gateway_attestation(validated: tuple[ValidatedSlot, ...]) -> bytes:
    return (
        "".join(
            f"{slot.slot_id}|{slot.container_path}|{slot.filesystem_id}|"
            f"{slot.root_inode}|read_only|{slot.mount_fingerprint}\n"
            for slot in validated
        )
    ).encode("ascii")


def _worker_command(role: str) -> list[str]:
    return [
        "/bin/sh",
        "-ec",
        f"aegisctl mounts attest --manifest {MANIFEST_TARGET} "
        f"--role {role} && exec python manage.py run_role --role {role}",
    ]


def render_artifacts(
    config_path: Path,
    manifest_path: Path,
    output_path: Path,
    gateway_attestation_path: Path,
    *,
    uid: int,
    gid: int,
) -> RenderResult:
    import yaml
    from aegis_apps.roots.manifest import MountManifest

    if (uid, gid) != (os.geteuid(), os.getegid()) or uid == 0 or gid == 0:
        raise ConfigError("render identity must match invoking identity")
    _ensure_distinct_artifacts(
        (config_path, manifest_path, output_path, gateway_attestation_path)
    )
    specs = parse_config(config_path)
    ensure_outputs_outside_originals(
        (output_path, gateway_attestation_path), tuple(spec.source for spec in specs)
    )
    manifest_raw = _read_bounded_regular(
        manifest_path, MAX_MANIFEST_BYTES, "mount manifest"
    )
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    try:
        manifest = MountManifest.load(manifest_path, manifest_digest)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    validated = _validated_render_slots(specs, {
        slot_id: _ManifestBinding(
            slot.container_path.as_posix(), slot.mode, slot.expected_identity,
            slot.filesystem_id, slot.root_inode, slot.mount_fingerprint,
        )
        for slot_id, slot in manifest.slots.items()
    })

    attestation_raw = _gateway_attestation(validated)
    gateway_digest = hashlib.sha256(attestation_raw).hexdigest()

    manifest_target = MANIFEST_TARGET
    attestation_target = GATEWAY_ATTESTATION_TARGET
    identity = f"{uid}:{gid}"
    backend_environment = {
        "AEGIS_RELEASE_ID": "${AEGIS_RELEASE_ID:?AEGIS_RELEASE_ID is required}",
        "AEGIS_MOUNT_MANIFEST": manifest_target,
        "AEGIS_MOUNT_MANIFEST_SHA256": manifest_digest,
    }
    service_volumes = {
        "migrate": [_bind(manifest_path.resolve(), manifest_target, read_only=True)],
        "web": [_bind(manifest_path.resolve(), manifest_target, read_only=True)],
        "operations": [_bind(manifest_path.resolve(), manifest_target, read_only=True)],
        "indexer": [_bind(manifest_path.resolve(), manifest_target, read_only=True)],
        "media": [_bind(manifest_path.resolve(), manifest_target, read_only=True)],
        "gateway": [
            _bind(
                gateway_attestation_path.resolve(),
                attestation_target,
                read_only=True,
            )
        ],
    }
    services: dict[str, dict[str, object]] = {
        "migrate": {
            "environment": {
                "AEGIS_MOUNT_MANIFEST": manifest_target,
                "AEGIS_MOUNT_MANIFEST_SHA256": manifest_digest,
            },
            "volumes": service_volumes["migrate"],
        },
        "web": {
            "user": identity,
            "environment": backend_environment,
            "volumes": service_volumes["web"],
        },
        **{
            role: {
                "user": identity,
                "environment": {**backend_environment, "AEGIS_PROCESS_ROLE": role},
                "command": _worker_command(role),
                "volumes": service_volumes[role],
            }
            for role in ("operations", "indexer", "media")
        },
        "gateway": {
            "environment": {
                "AEGIS_GATEWAY_MOUNT_ATTESTATION": attestation_target,
                "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": gateway_digest,
            },
            "volumes": service_volumes["gateway"],
        },
    }
    for slot in validated:
        service_volumes["gateway"].append(
            _bind(slot.source, slot.container_path, read_only=True)
        )
        service_volumes["indexer"].append(
            _bind(slot.source, slot.container_path, read_only=True)
        )
        service_volumes["media"].append(
            _bind(slot.source, slot.container_path, read_only=True)
        )
        service_volumes["operations"].append(
            _bind(slot.source, slot.container_path, read_only=True)
        )
    compose_raw = yaml.safe_dump(
        {"services": services},
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).encode("ascii")
    _atomic_write(gateway_attestation_path, attestation_raw, 0o644)
    _atomic_write(output_path, compose_raw, 0o644)
    return RenderResult(manifest_digest, gateway_digest)


def refuse_comma_sources(slots: Sequence[SlotSpec | ValidatedSlot]) -> None:
    """Podman's Docker-compatible API splits a bind source at commas into mount options."""
    for slot in slots:
        if "," in str(slot.source):
            raise _safe_slot_error(
                slot.slot_id,
                "Podman cannot bind-mount an original root whose path contains a comma",
            )


def refuse_podman_comma_sources(
    slots: Sequence[SlotSpec | ValidatedSlot], environment: Mapping[str, str] | None = None,
) -> None:
    """Refuse comma-bearing sources when Podman is (or may be) the selected engine."""
    source = os.environ if environment is None else environment
    # The same Docker short-circuit as the mask policy: Docker accepts commas unchanged.
    if source.get("AEGIS_CONTAINER_ENGINE", "docker") == "docker":
        return
    refuse_comma_sources(slots)


def _quadlet_literal(value: str) -> bool:
    return (
        value.startswith("/")
        and value.isascii()
        and value.isprintable()
        and _QUADLET_UNSAFE.isdisjoint(value)
    )


def _require_quadlet_sources(slots: Sequence[SlotSpec | ValidatedSlot]) -> None:
    """Refuse, before any write or container, a source no Quadlet unit can carry literally."""
    refuse_comma_sources(slots)
    for slot in slots:
        if not _quadlet_literal(str(slot.source)):
            raise _safe_slot_error(
                slot.slot_id,
                "a Quadlet original root path must not contain whitespace, control "
                "characters, quotes, backslashes, colons, '%' or '$'",
            )


def _quadlet_artifact(path: Path, label: str) -> str:
    value = str(path.resolve())
    if not _quadlet_literal(value):
        raise ConfigError(
            f"{label} path cannot be bound by a Quadlet unit: it must not contain whitespace, "
            "control characters, quotes, backslashes, commas, colons, '%' or '$'"
        )
    return value


def require_host_group(host_gid: int) -> int:
    """D5: the Podman deployment keeps fixed container users and maps only their group.

    The compose runtime identity (AEGIS_UID/AEGIS_GID) does not apply; the mapped host
    group must be the invoking user's own, non-root group.
    """
    if type(host_gid) is not int or not 0 < host_gid <= MAX_GROUP_ID:
        raise ConfigError("host group must be a non-root group ID")
    if os.geteuid() == 0:
        raise ConfigError("the rootless Podman deployment must not run as root")
    if host_gid != os.getegid():
        raise ConfigError("host group must match the invoking group")
    return host_gid


def _quadlet_exec(command: list[str]) -> str:
    words: list[str] = []
    for word in command:
        # Read literally by Quadlet's word splitting and by systemd's ExecStart expansion.
        if not _QUADLET_EXEC_WORD_RE.fullmatch(word):
            raise ConfigError("command cannot be written into a Quadlet unit")
        words.append(f'"{word}"' if " " in word else word)
    return " ".join(words)


def _quadlet_drop_in(
    environment: Mapping[str, str],
    volumes: Sequence[tuple[str, str]],
    command: list[str] | None = None,
) -> bytes:
    lines = [
        "# Generated by aegisctl mounts render --format quadlet. Do not edit.",
        "[Container]",
        *(f"Environment={key}={value}" for key, value in environment.items()),
        *([] if command is None else [f"Exec={_quadlet_exec(command)}"]),
        # No SELinux relabel option: originals are never relabeled (D6).
        *(f"Volume={source}:{target}:ro" for source, target in volumes),
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def render_quadlet_artifacts(
    config_path: Path,
    manifest_path: Path,
    output_dir: Path,
    gateway_attestation_path: Path,
    *,
    unit_prefix: str,
    host_gid: int,
) -> RenderResult:
    """Write per-unit Quadlet drop-ins carrying exactly what the compose render adds.

    Each ``<prefix><role>.container.d/50-aegis-roots.conf`` binds the manifest or the
    gateway attestation, the read-only roots and, for workers, the attestation ``Exec=``.
    Runs on a standard-library-only host Python.
    """
    if not isinstance(unit_prefix, str) or not UNIT_PREFIX_RE.fullmatch(unit_prefix):
        raise ConfigError("invalid Quadlet unit prefix")
    require_host_group(host_gid)
    drop_ins = {
        role: output_dir / f"{unit_prefix}{role}.container.d" / QUADLET_DROP_IN
        for role in QUADLET_ROLES
    }
    _ensure_distinct_artifacts(
        (config_path, manifest_path, gateway_attestation_path, *drop_ins.values())
    )
    specs = parse_config(config_path)
    _require_quadlet_sources(specs)
    manifest_source = _quadlet_artifact(manifest_path, "mount manifest")
    attestation_source = _quadlet_artifact(gateway_attestation_path, "gateway attestation")
    ensure_outputs_outside_originals(
        (output_dir, gateway_attestation_path, *drop_ins.values()),
        tuple(spec.source for spec in specs),
    )
    manifest_digest, bindings = _read_render_manifest(manifest_path)
    validated = _validated_render_slots(specs, bindings)
    # A configured source may resolve elsewhere; the unit carries the resolved form.
    _require_quadlet_sources(validated)
    attestation_raw = _gateway_attestation(validated)
    gateway_digest = hashlib.sha256(attestation_raw).hexdigest()

    manifest_environment = {
        "AEGIS_MOUNT_MANIFEST": MANIFEST_TARGET,
        "AEGIS_MOUNT_MANIFEST_SHA256": manifest_digest,
    }
    manifest_bind = (manifest_source, MANIFEST_TARGET)
    roots = [(str(slot.source), slot.container_path) for slot in validated]
    rendered = {
        "migrate": _quadlet_drop_in(manifest_environment, [manifest_bind]),
        "web": _quadlet_drop_in(manifest_environment, [manifest_bind]),
        **{
            role: _quadlet_drop_in(
                manifest_environment, [manifest_bind, *roots], _worker_command(role),
            )
            for role in ("operations", "indexer", "media")
        },
        "gateway": _quadlet_drop_in(
            {
                "AEGIS_GATEWAY_MOUNT_ATTESTATION": GATEWAY_ATTESTATION_TARGET,
                "AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256": gateway_digest,
            },
            [(attestation_source, GATEWAY_ATTESTATION_TARGET), *roots],
        ),
    }
    _atomic_write(gateway_attestation_path, attestation_raw, 0o644)
    for role, path in drop_ins.items():
        _atomic_write(path, rendered[role], 0o644)
    return RenderResult(manifest_digest, gateway_digest)


_MOUNTINFO_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def _decode_mountinfo_field(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        escape = value[index + 1 : index + 4]
        if len(escape) != 3 or escape not in _MOUNTINFO_ESCAPES:
            raise MountAttestationError("mountinfo contains an invalid escape")
        result.append(_MOUNTINFO_ESCAPES[escape])
        index += 4
    return "".join(result)


def _option_mode(options: str) -> Mode:
    values = options.split(",")
    has_ro = "ro" in values
    has_rw = "rw" in values
    if has_ro == has_rw:
        raise MountAttestationError("mountinfo contains ambiguous mount flags")
    return "read_only" if has_ro else "read_write"


def _parse_mountinfo_records(raw: bytes) -> tuple[MountInfoRecord, ...]:
    if len(raw) > MAX_MOUNTINFO_BYTES:
        raise MountAttestationError("mountinfo exceeds size limit")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MountAttestationError("mountinfo is malformed") from exc
    records: list[MountInfoRecord] = []
    lines = text.splitlines()
    if len(lines) > 8192:
        raise MountAttestationError("mountinfo exceeds record limit")
    for line in lines:
        if not line or len(line) > 16_384:
            raise MountAttestationError("mountinfo is malformed")
        fields = line.split(" ")
        separators = [index for index, field in enumerate(fields) if field == "-"]
        if len(separators) != 1 or separators[0] < 6 or len(fields) <= separators[0] + 3:
            raise MountAttestationError("mountinfo is malformed")
        mountpoint = _decode_mountinfo_field(fields[4])
        major_minor = fields[2]
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9}):(?:0|[1-9][0-9]{0,9})", major_minor):
            raise MountAttestationError("mountinfo is malformed")
        encoded_root = fields[3]
        filesystem_type = fields[separators[0] + 1]
        mount_source = fields[separators[0] + 2]
        if any(
            not value or len(value) > MAX_PATH_LENGTH
            for value in (encoded_root, filesystem_type, mount_source)
        ):
            raise MountAttestationError("mountinfo is malformed")
        decoded_root = _decode_mountinfo_field(encoded_root)
        filesystem_root = PurePosixPath(decoded_root)
        _decode_mountinfo_field(filesystem_type)
        _decode_mountinfo_field(mount_source)
        per_mount = _option_mode(fields[5])
        superblock = _option_mode(fields[separators[0] + 3])
        effective: Mode
        if per_mount == "read_only" or superblock == "read_only":
            effective = "read_only"
        elif per_mount == "read_write" and superblock == "read_write":
            effective = "read_write"
        else:
            raise MountAttestationError("mountinfo contains ambiguous mount flags")
        fingerprint_payload = b"aegis.mount-fingerprint.v1\0" + b"".join(
            value.encode("ascii") + b"\0"
            for value in (major_minor, encoded_root, filesystem_type, mount_source)
        )
        records.append(MountInfoRecord(
            mountpoint,
            effective,
            hashlib.sha256(fingerprint_payload).hexdigest(),
            (major_minor, filesystem_type),
            filesystem_root,
        ))
    return tuple(records)


def parse_mountinfo(raw: bytes) -> Mapping[str, MountInfoRecord]:
    records: dict[str, MountInfoRecord] = {}
    for record in _parse_mountinfo_records(raw):
        if record.mountpoint in records:
            raise MountAttestationError("mountinfo contains an ambiguous mountpoint")
        records[record.mountpoint] = record
    return MappingProxyType(records)


def _host_mountinfo() -> tuple[MountInfoRecord, ...] | None:
    if sys.platform == "darwin":
        return None
    if sys.platform != "linux":
        raise ConfigError("host mount topology cannot be checked")
    try:
        with Path("/proc/self/mountinfo").open("rb") as handle:
            return _parse_mountinfo_records(handle.read(MAX_MOUNTINFO_BYTES + 1))
    except (OSError, MountAttestationError) as exc:
        raise ConfigError("host mount identity cannot be checked") from exc


def _host_mountpoints(records: tuple[MountInfoRecord, ...] | None) -> tuple[Path, ...]:
    if records is not None:
        return tuple(Path(record.mountpoint) for record in records)
    try:
        return darwin_mountpoints()
    except HostTopologyError as exc:
        raise ConfigError("host mount topology cannot be checked") from exc


def _host_path_forms(path: Path) -> frozenset[Path]:
    if sys.platform != "darwin":
        return frozenset({path.resolve(strict=False)})
    try:
        return darwin_path_forms(path)
    except HostTopologyError as exc:
        raise ConfigError("host mount topology cannot be checked") from exc


def _reject_nested_mounts(roots: frozenset[Path], mountpoints: tuple[Path, ...]) -> None:
    if any(root in mountpoint.parents for root in roots for mountpoint in mountpoints):
        raise ConfigError(
            "original root contains a nested mount; declare non-overlapping leaf roots"
        )


def _physical_location(
    path: Path, records: tuple[MountInfoRecord, ...],
) -> tuple[tuple[str, str], PurePosixPath]:
    target = PurePosixPath(path)
    candidates = [record for record in records
                  if target.is_relative_to(PurePosixPath(record.mountpoint))]
    # Host mount tables can contain legitimate stacks at unrelated paths. Keep
    # those records; never guess stack visibility from file order on our path.
    mountpoints = [record.mountpoint for record in candidates]
    if (not candidates or len(mountpoints) != len(set(mountpoints))
            or any(not _has_physical_root(record) for record in candidates)):
        raise ConfigError("host mount identity cannot be checked")
    record = max(candidates, key=lambda item: len(PurePosixPath(item.mountpoint).parts))
    return (
        record.filesystem_identity,
        record.filesystem_root / target.relative_to(record.mountpoint),
    )


def _has_physical_root(record: MountInfoRecord) -> bool:
    # Kernel fields may be opaque (e.g. inherited cgroup roots reported as /..).
    # Preserve them when parsing; require usable ancestry only for our paths.
    return record.filesystem_root.is_absolute() and ".." not in record.filesystem_root.parts


def _physical_overlap(
    first: tuple[tuple[str, str], PurePosixPath],
    second: tuple[tuple[str, str], PurePosixPath],
) -> bool:
    return first[0] == second[0] and (
        first[1].is_relative_to(second[1]) or second[1].is_relative_to(first[1])
    )


def attest_mounts(
    manifest: MountManifest,
    role: Literal["operations", "indexer", "media"],
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    try:
        with mountinfo_path.open("rb") as handle:
            raw = handle.read(MAX_MOUNTINFO_BYTES + 1)
    except OSError as exc:
        raise MountAttestationError("mountinfo cannot be read") from exc
    records = parse_mountinfo(raw)
    for slot in manifest.slots.values():
        record = records.get(slot.container_path.as_posix())
        if (
            record is None
            or any(slot.container_path in PurePosixPath(target).parents for target in records)
            or record.effective_mode != "read_only"
            or not _has_physical_root(record)
            or not secrets.compare_digest(
                record.mount_fingerprint, slot.mount_fingerprint
            )
        ):
            raise MountAttestationError(f"mount attestation failed for slot {slot.slot_id}")
        descriptor = -1
        try:
            if not os.access(slot.container_path, os.R_OK | os.X_OK, effective_ids=True):
                raise MountAttestationError(f"mount attestation failed for slot {slot.slot_id}")
            descriptor = os.open(
                slot.container_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise MountAttestationError(f"mount attestation failed for slot {slot.slot_id}")
        except OSError as exc:
            raise MountAttestationError(
                f"mount attestation failed for slot {slot.slot_id}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _observer_path_identity(path: Path, *, directory: bool) -> ObserverPathIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfigError("observer diagnostic inventory changed") from exc
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if (
        stat.S_IFMT(metadata.st_mode) != expected_type
        or metadata.st_uid != os.geteuid()
        or (not directory and metadata.st_nlink != 1)
    ):
        raise ConfigError("observer diagnostic inventory changed")
    return ObserverPathIdentity(
        metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode),
        metadata.st_uid, metadata.st_gid, metadata.st_nlink,
    )


def _validate_observer_diagnostics(
    root: Path, expected: Mapping[Path, ObserverPathIdentity],
) -> None:
    if expected.get(root) != _observer_path_identity(root, directory=True):
        raise ConfigError("observer diagnostic inventory changed")
    try:
        current = {root, *(Path(entry.path) for entry in os.scandir(root))}
    except OSError as exc:
        raise ConfigError("observer diagnostic inventory changed") from exc
    if current != set(expected):
        raise ConfigError("observer diagnostic inventory changed")
    for path, identity in expected.items():
        if path == root:
            continue
        if _observer_path_identity(path, directory=False) != identity:
            raise ConfigError("observer diagnostic inventory changed")


def _remove_observer_diagnostics(
    root: Path, expected: Mapping[Path, ObserverPathIdentity],
) -> None:
    _validate_observer_diagnostics(root, expected)
    for path in sorted((path for path in expected if path != root), key=str):
        path.unlink()
    if _observer_path_identity(root, directory=True) != expected[root]:
        raise ConfigError("observer diagnostic inventory changed")
    try:
        if any(os.scandir(root)):
            raise ConfigError("observer diagnostic inventory changed")
        root.rmdir()
    except OSError as exc:
        raise ConfigError("observer diagnostic inventory changed") from exc


def _retained_observer_error(root: Path) -> ConfigError:
    return ConfigError(f"container mount observation failed; diagnostics retained at {root}")


def _observer_script(slots: tuple[ValidatedSlot, ...]) -> str:
    targets = [slot.container_path for slot in slots]
    target_expression = " || ".join(
        f'($5 == "{target}" || index($5, "{target}/") == 1)' for target in targets
    )
    return (
        "awk '"
        "length($0) > 16384 { exit 65 } "
        "NR > 8192 { exit 66 } "
        f"({target_expression}) {{ print; matched++; if (matched > {len(slots) * 2}) exit 67 }}"
        "' /proc/self/mountinfo"
    )


def _observer_temp_parent(slots: tuple[ValidatedSlot, ...]) -> Path:
    # gettempdir() may itself create a write probe. Select and validate the
    # configured location before asking tempfile to create anything.
    temp_parent = Path(tempfile.tempdir or next(
        (os.environ[name] for name in ("TMPDIR", "TEMP", "TMP") if os.environ.get(name)),
        "/tmp",
    ))
    ensure_outputs_outside_originals((temp_parent,), tuple(slot.source for slot in slots))
    return temp_parent


def _run_observer(
    slots: tuple[ValidatedSlot, ...],
    temp: Path,
    diagnostics: Mapping[Path, ObserverPathIdentity],
    output_path: Path,
    run: Callable[[BinaryIO], bool],
    cleanup: Callable[[], None],
) -> tuple[ValidatedSlot, ...]:
    """Run one bounded observer, prove cleanup, then validate or retain its diagnostics.

    ``run`` writes at most the observer limit to the open output file and reports success.
    """
    run_succeeded = False
    run_error: BaseException | None = None
    try:
        with output_path.open("wb") as observer_output:
            run_succeeded = run(observer_output)
    except BaseException as exc:
        run_error = exc
    try:
        _validate_observer_diagnostics(temp, diagnostics)
        cleanup()
    except BaseException as recovery_error:
        if isinstance(run_error, (SystemExit, KeyboardInterrupt)):
            run_error.add_note(str(_retained_observer_error(temp)))
            raise run_error from recovery_error
        if isinstance(recovery_error, (SystemExit, KeyboardInterrupt)):
            recovery_error.add_note(str(_retained_observer_error(temp)))
            raise
        raise _retained_observer_error(temp) from recovery_error
    if run_error is not None:
        if isinstance(run_error, (SystemExit, KeyboardInterrupt)):
            run_error.add_note(str(_retained_observer_error(temp)))
            raise run_error
        raise _retained_observer_error(temp) from run_error
    try:
        if not run_succeeded:
            raise ConfigError("container mount observation failed")
        with output_path.open("rb") as observer_output:
            raw = observer_output.read(MAX_MOUNTINFO_BYTES + 1)
        observed = _validated_observer_slots(slots, raw)
        _remove_observer_diagnostics(temp, diagnostics)
    except (SystemExit, KeyboardInterrupt) as exc:
        exc.add_note(str(_retained_observer_error(temp)))
        raise
    except (OSError, ConfigError) as exc:
        raise _retained_observer_error(temp) from exc
    return observed


def observe_mount_fingerprints(
    slots: tuple[ValidatedSlot, ...],
) -> tuple[ValidatedSlot, ...]:
    import yaml

    if not slots:
        raise ConfigError("mount slot observation requires slots")
    # Before the mask-compatibility probes or the observer create any container.
    refuse_podman_comma_sources(slots)
    temp_parent = _observer_temp_parent(slots)
    try:
        mask_options = require_podman_mask_compatibility(
            originals=tuple(slot.source for slot in slots),
        )
    except PodmanMaskError as exc:
        raise ConfigError(str(exc)) from exc
    project_name = f"aegis-preflight-{secrets.token_hex(8)}"
    observer_script = _observer_script(slots)
    service = {
        "image": "aegis-gateway",
        "pull_policy": "never",
        "user": "101:101",
        "network_mode": "none",
        "read_only": True,
        "cpus": 0.5,
        "mem_limit": "64m",
        "pids_limit": 64,
        "stop_grace_period": "3s",
        # One-off: never started again by an engine restart or a host boot.
        "restart": "no",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true", *mask_options],
        "entrypoint": ["/bin/sh", "-eu", "-c"],
        "command": [observer_script],
        "volumes": [
            _bind(slot.source, slot.container_path, read_only=True) for slot in slots
        ],
    }
    try:
        temp = Path(tempfile.mkdtemp(prefix="aegis-mount-preflight-", dir=temp_parent))
        diagnostics: dict[Path, ObserverPathIdentity] = {
            temp: _observer_path_identity(temp, directory=True),
        }
        compose_path = temp / "compose.yaml"
        output_path = temp / "mountinfo.out"
        compose_raw = yaml.safe_dump(
            {"services": {"mount-observer": service}},
            sort_keys=False,
            allow_unicode=False,
        ).encode("ascii")
        _atomic_write(compose_path, compose_raw, 0o600)
        diagnostics[compose_path] = _observer_path_identity(compose_path, directory=False)
        _atomic_write(output_path, b"", 0o600)
        diagnostics[output_path] = _observer_path_identity(output_path, directory=False)
        command = compose_command(
            "--project-name",
            project_name,
            "-f",
            str(compose_path),
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "--no-TTY",
            "mount-observer",
        )
        try:
            expected_resources = require_empty_project(project_name)
        except ProjectResourceError as exc:
            raise _retained_observer_error(temp) from exc

        def run_compose(observer_output: BinaryIO) -> bool:
            # The limit lands on the Compose client; the engine writes container state.
            result = subprocess.run(
                command,
                env=compose_environment(os.environ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=observer_output,
                stderr=subprocess.DEVNULL,
                preexec_fn=set_observer_output_limit,
                timeout=OBSERVER_TIMEOUT_SECONDS,
            )
            return result.returncode == 0

        return _run_observer(
            slots, temp, diagnostics, output_path, run_compose,
            lambda: _cleanup_observer_project(project_name, expected_resources),
        )
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("container mount observation failed") from exc


def _host_podman_environment() -> dict[str, str]:
    """The plain podman CLI on PATH, never routed to a remote service."""
    return {key: value for key, value in os.environ.items() if key not in REMOTE_VARIABLES}


def _podman_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [*_PODMAN, *arguments], env=_host_podman_environment(), check=False,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # ValueError: text=True cannot decode the reply.
        raise ConfigError("observer container state cannot be checked") from exc


def _copy_observer_streams(
    command: list[str], stdout: IO[bytes], stderr: IO[bytes],
    output: BinaryIO, errors: BinaryIO, deadline: float,
) -> bool:
    """Copy both pipes until they close; False as soon as stdout exceeds the observer limit."""
    stdout_descriptor = stdout.fileno()
    bounds = {
        stdout_descriptor: (output, MAX_MOUNTINFO_BYTES + 1),
        stderr.fileno(): (errors, OBSERVER_STDERR_LIMIT),
    }
    copied = dict.fromkeys(bounds, 0)
    with selectors.DefaultSelector() as selector:
        for descriptor in bounds:
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, OBSERVER_TIMEOUT_SECONDS)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                target, limit = bounds[key.fd]
                kept = chunk[: max(limit - copied[key.fd], 0)]
                if kept:
                    target.write(kept)
                copied[key.fd] += len(chunk)
                if key.fd == stdout_descriptor and copied[key.fd] > limit:
                    return False
                # Stderr past its bound is drained and dropped, so the client never blocks.
    return True


def _bounded_observer_run(command: list[str], output: BinaryIO, errors: BinaryIO) -> bool:
    """Run the podman client with no inherited file-size limit; bound its output here.

    Rootless podman is the engine: it writes its own database and container storage, so
    RLIMIT_FSIZE must never reach it. The parent copies at most the observer limit and
    kills the client past it, at the deadline or on interruption. The caller then removes
    the run's exact container.
    """
    deadline = time.monotonic() + OBSERVER_TIMEOUT_SECONDS
    process = subprocess.Popen(
        command, env=_host_podman_environment(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        if process.stdout is None or process.stderr is None:
            raise ConfigError("container mount observation failed")
        if not _copy_observer_streams(
            command, process.stdout, process.stderr, output, errors, deadline,
        ):
            process.kill()
            process.wait()
            return False
        return process.wait(timeout=max(deadline - time.monotonic(), 0.0)) == 0
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _observer_container_present(name: str) -> bool:
    result = _podman_cli("container", "exists", name)
    if result.returncode not in (0, 1):
        raise ConfigError("observer container state cannot be checked")
    return result.returncode == 0


def _remove_observer_container(name: str, token: str) -> None:
    """Remove only this run's container: its exact name, label token, then immutable ID."""
    if not _observer_container_present(name):
        return
    inspected = _podman_cli("container", "inspect", name)
    if inspected.returncode:
        if not _observer_container_present(name):
            return  # Its own --rm removed it meanwhile.
        raise ConfigError("observer container cannot be inspected")
    try:
        payload: Any = json.loads(inspected.stdout)
    except ValueError as exc:
        raise ConfigError("observer container cannot be inspected") from exc
    info = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
    config = info.get("Config") if isinstance(info, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    identifier = info.get("Id") if isinstance(info, dict) else None
    if (
        not isinstance(info, dict)
        or info.get("Name") != name
        or not isinstance(labels, dict)
        or labels.get(OBSERVER_LABEL) != token
        or not isinstance(identifier, str)
        or not _CONTAINER_ID_RE.fullmatch(identifier)
    ):
        raise ConfigError("observer container identity changed")
    _podman_cli("rm", "--force", identifier)
    if _observer_container_present(name):
        raise ConfigError("observer container cleanup was incomplete")


def observe_mount_fingerprints_podman_run(
    slots: tuple[ValidatedSlot, ...],
    *,
    image: str,
    host_gid: int,
    reader_type: str,
    allow_test_image_id: bool = False,
) -> tuple[ValidatedSlot, ...]:
    """Observe fingerprints as the Podman deployment's readers will see the roots.

    Runs the constrained observer script with plain ``podman run`` under the production
    group mapping (D5) and SELinux reader type (D6). Production start excludes the
    development-time mask guard and its private API socket (D11), and so does this.
    """
    if not slots:
        raise ConfigError("mount slot observation requires slots")
    if not isinstance(image, str) or not (
        OBSERVER_IMAGE_RE.fullmatch(image)
        or (allow_test_image_id and LOCAL_IMAGE_ID_RE.fullmatch(image))
    ):
        raise ConfigError("observer image must be a name@sha256 digest reference")
    if not isinstance(reader_type, str) or not READER_TYPE_RE.fullmatch(reader_type):
        raise ConfigError("invalid observer SELinux reader type")
    require_host_group(host_gid)
    # Before any container: Podman's -v and the later Quadlet units read each source literally.
    _require_quadlet_sources(slots)
    temp_parent = _observer_temp_parent(slots)
    name = f"aegis-preflight-{secrets.token_hex(8)}"
    token = secrets.token_hex(16)
    command = [
        *_PODMAN, "run", "--rm", "--pull", "never",
        "--name", name, "--label", f"{OBSERVER_LABEL}={token}",
        "--network", "none", "--read-only",
        "--user", f"{OBSERVER_USER}:{OBSERVER_USER}", "--cap-drop", "all",
        "--security-opt", "no-new-privileges",
        "--security-opt", MASK_OPTION,
        "--security-opt", f"label=type:{reader_type}",
        "--gidmap", f"+g{OBSERVER_USER}:@{host_gid}:1",
        "--cpus", "0.5", "--memory", "64m", "--pids-limit", "64",
        # Compose's stop_grace_period: a forced removal waits 3 s, not Podman's 10 s.
        "--stop-timeout", "3",
        "--entrypoint", "/bin/sh",
        *(item for slot in slots for item in ("-v", f"{slot.source}:{slot.container_path}:ro")),
        image, "-eu", "-c", _observer_script(slots),
    ]
    try:
        temp = Path(tempfile.mkdtemp(prefix="aegis-mount-preflight-", dir=temp_parent))
        diagnostics: dict[Path, ObserverPathIdentity] = {
            temp: _observer_path_identity(temp, directory=True),
        }
        # Retained on failure: what ran, what it printed and the engine's own report.
        argv_path = temp / "observer.argv.json"
        output_path = temp / "mountinfo.out"
        errors_path = temp / "observer.stderr"
        _atomic_write(argv_path, (json.dumps(command, indent=1) + "\n").encode("ascii"), 0o600)
        for path in (output_path, errors_path):
            _atomic_write(path, b"", 0o600)
        for path in (argv_path, output_path, errors_path):
            diagnostics[path] = _observer_path_identity(path, directory=False)
        try:
            if _observer_container_present(name):
                raise ConfigError("observer container name is already in use")
        except ConfigError as exc:
            raise _retained_observer_error(temp) from exc

        def run_podman(observer_output: BinaryIO) -> bool:
            with errors_path.open("wb") as observer_errors:
                return _bounded_observer_run(command, observer_output, observer_errors)

        return _run_observer(
            slots, temp, diagnostics, output_path, run_podman,
            lambda: _remove_observer_container(name, token),
        )
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("container mount observation failed") from exc


def _validated_observer_slots(
    slots: tuple[ValidatedSlot, ...], raw: bytes,
) -> tuple[ValidatedSlot, ...]:
    try:
        records = parse_mountinfo(raw)
    except MountAttestationError as exc:
        raise ConfigError("container mount observation failed") from exc
    observed: list[ValidatedSlot] = []
    fingerprints: set[str] = set()
    physical_roots: list[tuple[tuple[str, str], PurePosixPath]] = []
    for slot in slots:
        _reject_nested_mounts(
            frozenset({Path(slot.container_path)}), tuple(Path(target) for target in records),
        )
        record = records.get(slot.container_path)
        if (record is None or record.effective_mode != "read_only"
                or not _has_physical_root(record)):
            raise ConfigError(f"mount slot {slot.slot_id}: container observation failed")
        if record.mount_fingerprint in fingerprints:
            raise ConfigError(f"mount slot {slot.slot_id}: inconsistent mount fingerprint")
        physical = (record.filesystem_identity, record.filesystem_root)
        if any(_physical_overlap(physical, existing) for existing in physical_roots):
            raise ConfigError(f"mount slot {slot.slot_id}: physical source overlap")
        physical_roots.append(physical)
        fingerprints.add(record.mount_fingerprint)
        observed.append(
            ValidatedSlot(
                slot.slot_id,
                slot.source,
                slot.container_path,
                slot.mode,
                slot.filesystem_id,
                slot.root_inode,
                slot.expected_identity,
                record.mount_fingerprint,
            )
        )
    if len(records) != len(slots):
        raise ConfigError("container mount observation is ambiguous")
    return tuple(observed)


def _cleanup_observer_project(
    project_name: str, expected: ProjectInventory,
) -> None:
    created = admit_project_transition(
        expected, capture_project_inventory(project_name), (
            ProjectResourceRule("container", (
                ("com.docker.compose.service", "mount-observer"),
                ("com.docker.compose.oneoff", "True"),
            )),
            ProjectResourceRule("network", (
                ("com.docker.compose.network", "default"),
            )),
        ),
    )
    cleanup_project_inventory(created)


def set_observer_output_limit() -> None:
    limit = MAX_MOUNTINFO_BYTES + 1
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
