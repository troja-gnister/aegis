from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import secrets
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

import yaml

from aegisctl.container_engine import (
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
SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LOCAL_IDENTITY_RE = re.compile(r"^local:(0|[1-9][0-9]{0,18}):(0|[1-9][0-9]{0,18})$")
REMOTE_IDENTITY_RE = re.compile(
    r"^remote:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?:"
    r"/[A-Za-z0-9._~!$&'()+,;=:@%/-]{1,383}$"
)


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


def _read_bounded_regular(path: Path, limit: int, label: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{label} is not a regular file")
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


def render_artifacts(
    config_path: Path,
    manifest_path: Path,
    output_path: Path,
    gateway_attestation_path: Path,
    *,
    uid: int,
    gid: int,
) -> RenderResult:
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
    if set(manifest.slots) != {spec.slot_id for spec in specs}:
        raise ConfigError("mount manifest does not match config")

    validated: list[ValidatedSlot] = []
    for spec in sorted(specs, key=lambda item: item.slot_id):
        manifest_slot = manifest.get(spec.slot_id)
        if manifest_slot is None:
            raise ConfigError(f"mount slot {spec.slot_id}: manifest mismatch")
        if (
            manifest_slot.container_path.as_posix() != spec.container_path
            or manifest_slot.mode != spec.mode
            or manifest_slot.expected_identity != spec.expected_identity
        ):
            raise ConfigError(f"mount slot {spec.slot_id}: manifest mismatch")
        try:
            resolved = spec.source.resolve(strict=True)
            info = resolved.stat()
        except OSError as exc:
            raise ConfigError(f"mount slot {spec.slot_id}: source is inaccessible") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError(f"mount slot {spec.slot_id}: source is not a directory")
        if (info.st_dev, info.st_ino) != (
            manifest_slot.filesystem_id,
            manifest_slot.root_inode,
        ):
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
                manifest_slot.mount_fingerprint,
            )
        )

    attestation_raw = (
        "".join(
            f"{slot.slot_id}|{slot.container_path}|{slot.filesystem_id}|"
            f"{slot.root_inode}|read_only|{slot.mount_fingerprint}\n"
            for slot in validated
        )
    ).encode("ascii")
    gateway_digest = hashlib.sha256(attestation_raw).hexdigest()

    manifest_target = "/run/aegis/mounts.manifest.json"
    attestation_target = "/run/aegis/mounts.gateway.attestation"
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
        "operations": {
            "user": identity,
            "environment": {
                **backend_environment,
                "AEGIS_PROCESS_ROLE": "operations",
            },
            "command": [
                "/bin/sh",
                "-ec",
                "aegisctl mounts attest --manifest /run/aegis/mounts.manifest.json "
                "--role operations && exec python manage.py run_role --role operations",
            ],
            "volumes": service_volumes["operations"],
        },
        "indexer": {
            "user": identity,
            "environment": {
                **backend_environment,
                "AEGIS_PROCESS_ROLE": "indexer",
            },
            "command": [
                "/bin/sh",
                "-ec",
                "aegisctl mounts attest --manifest /run/aegis/mounts.manifest.json "
                "--role indexer && exec python manage.py run_role --role indexer",
            ],
            "volumes": service_volumes["indexer"],
        },
        "media": {
            "user": identity,
            "environment": {
                **backend_environment,
                "AEGIS_PROCESS_ROLE": "media",
            },
            "command": [
                "/bin/sh",
                "-ec",
                "aegisctl mounts attest --manifest /run/aegis/mounts.manifest.json "
                "--role media && exec python manage.py run_role --role media",
            ],
            "volumes": service_volumes["media"],
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


def observe_mount_fingerprints(
    slots: tuple[ValidatedSlot, ...],
) -> tuple[ValidatedSlot, ...]:
    if not slots:
        raise ConfigError("mount slot observation requires slots")
    project_name = f"aegis-preflight-{secrets.token_hex(8)}"
    targets = [slot.container_path for slot in slots]
    target_expression = " || ".join(
        f'($5 == "{target}" || index($5, "{target}/") == 1)' for target in targets
    )
    observer_script = (
        "awk '"
        "length($0) > 16384 { exit 65 } "
        "NR > 8192 { exit 66 } "
        f"({target_expression}) {{ print; matched++; if (matched > {len(slots) * 2}) exit 67 }}"
        "' /proc/self/mountinfo"
    )
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
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "entrypoint": ["/bin/sh", "-eu", "-c"],
        "command": [observer_script],
        "volumes": [
            _bind(slot.source, slot.container_path, read_only=True) for slot in slots
        ],
    }
    try:
        # gettempdir() may itself create a write probe. Select and validate the
        # configured location before asking tempfile to create anything.
        temp_parent = Path(tempfile.tempdir or next(
            (os.environ[name] for name in ("TMPDIR", "TEMP", "TMP") if os.environ.get(name)),
            "/tmp",
        ))
        ensure_outputs_outside_originals((temp_parent,), tuple(slot.source for slot in slots))
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
        run_succeeded = False
        run_error: BaseException | None = None
        try:
            with output_path.open("wb") as observer_output:
                result = subprocess.run(
                    command,
                    env=compose_environment(os.environ),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=observer_output,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=set_observer_output_limit,
                    timeout=30,
                )
            run_succeeded = result.returncode == 0
        except BaseException as exc:
            run_error = exc
        try:
            _validate_observer_diagnostics(temp, diagnostics)
            _cleanup_observer_project(project_name, expected_resources)
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
