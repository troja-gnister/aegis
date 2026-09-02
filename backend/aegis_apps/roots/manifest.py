from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

from aegisctl.mounts import (
    MAX_INTEGER,
    MAX_SLOTS,
    SLOT_ID_RE,
    validate_expected_identity,
)

Mode = Literal["read_only", "read_write"]
MAX_MANIFEST_BYTES = 256 * 1024
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """A mount manifest is untrusted or inconsistent."""


@dataclass(frozen=True, slots=True)
class ManifestSlot:
    slot_id: str
    container_path: PurePosixPath
    mode: Mode
    expected_identity: str
    filesystem_id: int
    root_inode: int


def _integer(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ManifestError("invalid mount slot integer")
    return value


def parse_manifest_slot(item: object) -> ManifestSlot:
    if not isinstance(item, dict) or set(item) != {
        "slotId",
        "containerPath",
        "mode",
        "filesystemId",
        "rootInode",
        "expectedIdentity",
    }:
        raise ManifestError("invalid mount slot schema")
    slot_id = item["slotId"]
    if not isinstance(slot_id, str) or not SLOT_ID_RE.fullmatch(slot_id):
        raise ManifestError("invalid mount slot ID")
    expected_path = f"/srv/aegis/roots/{slot_id}"
    if item["containerPath"] != expected_path:
        raise ManifestError(f"invalid container path for slot {slot_id}")
    mode = item["mode"]
    if mode not in ("read_only", "read_write"):
        raise ManifestError(f"invalid mode for slot {slot_id}")
    try:
        identity = validate_expected_identity(item["expectedIdentity"])
    except ValueError:
        raise ManifestError(f"invalid identity for slot {slot_id}") from None
    filesystem_id = _integer(item["filesystemId"], allow_zero=True)
    root_inode = _integer(item["rootInode"], allow_zero=False)
    if identity.startswith("local:") and identity != f"local:{filesystem_id}:{root_inode}":
        raise ManifestError(f"invalid identity for slot {slot_id}")
    return ManifestSlot(
        slot_id,
        PurePosixPath(expected_path),
        cast(Mode, mode),
        identity,
        filesystem_id,
        root_inode,
    )


@dataclass(frozen=True, slots=True)
class MountManifest:
    digest: str
    slots: Mapping[str, ManifestSlot]

    @classmethod
    def load(cls, path: Path, expected_digest: str) -> MountManifest:
        if not isinstance(expected_digest, str) or not DIGEST_RE.fullmatch(expected_digest):
            raise ManifestError("invalid mount manifest digest")
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
        except OSError as exc:
            try:
                is_symlink = path.is_symlink()
            except OSError:
                is_symlink = False
            message = (
                "mount manifest is not a regular file"
                if is_symlink
                else "mount manifest cannot be read"
            )
            raise ManifestError(message) from exc
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise ManifestError("mount manifest is not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.close(descriptor)
            raise ManifestError("mount manifest has invalid permissions")
        if (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
            os.close(descriptor)
            raise ManifestError("mount manifest has invalid owner")
        try:
            chunks: list[bytes] = []
            remaining = MAX_MANIFEST_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError as exc:
            raise ManifestError("mount manifest cannot be read") from exc
        finally:
            os.close(descriptor)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError("mount manifest exceeds size limit")
        digest = hashlib.sha256(raw).hexdigest()
        if not secrets.compare_digest(digest, expected_digest):
            raise ManifestError("mount manifest digest mismatch")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError("invalid mount manifest JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"version", "generatedAt", "slots"}:
            raise ManifestError("unsupported mount manifest schema")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ManifestError("unsupported mount manifest schema")
        generated = payload["generatedAt"]
        if not isinstance(generated, str) or len(generated) > 40 or not generated.endswith("Z"):
            raise ManifestError("invalid mount manifest timestamp")
        try:
            datetime.fromisoformat(generated.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManifestError("invalid mount manifest timestamp") from exc
        items = payload["slots"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_SLOTS:
            raise ManifestError("invalid mount manifest slot count")
        slots: dict[str, ManifestSlot] = {}
        identities: set[str] = set()
        observed: set[tuple[int, int]] = set()
        for item in items:
            slot = parse_manifest_slot(item)
            pair = (slot.filesystem_id, slot.root_inode)
            if (
                slot.slot_id in slots
                or slot.expected_identity in identities
                or pair in observed
            ):
                raise ManifestError(f"duplicate mount slot {slot.slot_id}")
            slots[slot.slot_id] = slot
            identities.add(slot.expected_identity)
            observed.add(pair)
        return cls(digest=digest, slots=MappingProxyType(slots))

    def get(self, slot_id: str) -> ManifestSlot | None:
        return self.slots.get(slot_id)
