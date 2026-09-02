from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

Mode = Literal["read_only", "read_write"]

MAX_CONFIG_BYTES = 64 * 1024
MAX_SLOTS = 128
MAX_PATH_LENGTH = 4096
MAX_IDENTITY_LENGTH = 512
MAX_INTEGER = (1 << 63) - 1
SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LOCAL_IDENTITY_RE = re.compile(r"^local:(0|[1-9][0-9]{0,18}):(0|[1-9][0-9]{0,18})$")
REMOTE_IDENTITY_RE = re.compile(
    r"^remote:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?:"
    r"/[A-Za-z0-9._~!$&'()+,;=:@%/-]{1,383}$"
)


class ConfigError(ValueError):
    """A bounded, safe mount configuration error."""


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


def parse_config(path: Path) -> tuple[SlotSpec, ...]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ConfigError("mount config is not a regular file")
        raw = path.read_bytes()
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("mount config cannot be read") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("mount config exceeds size limit")
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


def _writable_probe(root_fd: int, slot_id: str) -> None:
    reserved = ".aegis-preflight"
    created_reserved = False
    probe_fd = -1
    first_name = f"probe-{secrets.token_hex(16)}"
    second_name = f"probe-{secrets.token_hex(16)}"
    owned_names: set[str] = set()
    try:
        try:
            os.mkdir(reserved, mode=0o700, dir_fd=root_fd)
            created_reserved = True
        except FileExistsError:
            info = os.stat(reserved, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ConfigError(
                    f"mount slot {slot_id}: writable probe unavailable"
                ) from None
        probe_fd = os.open(
            reserved,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.listdir(probe_fd):
            raise ConfigError(f"mount slot {slot_id}: writable probe area is not empty")
        file_fd = os.open(
            first_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=probe_fd,
        )
        owned_names.add(first_name)
        try:
            os.write(file_fd, b"aegis-preflight-v1\n")
            os.fsync(file_fd)
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(file_fd)
        os.rename(first_name, second_name, src_dir_fd=probe_fd, dst_dir_fd=probe_fd)
        owned_names.remove(first_name)
        owned_names.add(second_name)
        os.fsync(probe_fd)
        os.unlink(second_name, dir_fd=probe_fd)
        owned_names.remove(second_name)
        os.fsync(probe_fd)
    except ConfigError:
        raise
    except (OSError, BlockingIOError) as exc:
        raise ConfigError(f"mount slot {slot_id}: writable probe failed") from exc
    finally:
        if probe_fd >= 0:
            for name in tuple(owned_names):
                with suppress(OSError):
                    os.unlink(name, dir_fd=probe_fd)
            os.close(probe_fd)
        if created_reserved:
            with suppress(OSError):
                os.rmdir(reserved, dir_fd=root_fd)


def preflight_slots(slots: list[SlotSpec] | tuple[SlotSpec, ...]) -> tuple[ValidatedSlot, ...]:
    if not 1 <= len(slots) <= MAX_SLOTS:
        raise ConfigError("invalid mount slot count")
    resolved_slots: list[ValidatedSlot] = []
    seen_paths: set[Path] = set()
    seen_stat: set[tuple[int, int]] = set()
    seen_ids: set[str] = set()
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
        if resolved in seen_paths or identity_pair in seen_stat:
            raise ConfigError(f"mount slot {checked.slot_id}: duplicate source")
        for existing in seen_paths:
            if resolved in existing.parents or existing in resolved.parents:
                raise ConfigError(f"mount slot {checked.slot_id}: source overlap")
        seen_paths.add(resolved)
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
            if checked.mode == "read_write":
                _writable_probe(root_fd, checked.slot_id)
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
            }
            for slot in sorted(slots, key=lambda item: item.slot_id)
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    _atomic_write(path, raw, 0o600)
    return hashlib.sha256(raw).hexdigest()
